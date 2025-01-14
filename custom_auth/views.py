import base64
from io import BytesIO

import pyotp
import qrcode
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetCompleteView, PasswordResetConfirmView, \
    PasswordResetView, PasswordResetDoneView
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy

from custom_auth.form import (
    EmailChangeForm,
    GroupAddForm,
    GroupForm,
    MyPasswordChangeForm,
    ProfileEditForm,
    TwoAuthForm, ForgetForm, NewSetPasswordForm,
)
from custom_auth.models import UserGroup
from custom_auth.models import TOTPDevice, UserActivateToken


@login_required
def index(request):
    context = {}
    return render(request, "user/profile.html", context)


@login_required
def password_change(request):
    form = MyPasswordChangeForm(user=request.user, data=request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            return render(request, "done.html", {"text": "パスワードの変更を行いました"})
    context = {"form": form}
    return render(request, "user/change_password.html", context)


@login_required
def change_email(request):
    form = EmailChangeForm(data=request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            form.save(user=request.user)
            return render(request, "done.html", {"text": "メールアドレスの変更を行いました"})
    return render(request, "user/change_email.html", {"form": form})


@login_required
def edit_profile(request):
    form = ProfileEditForm(data=request.POST or None)
    userdata = {
        "username": request.user.username,
        "username_jp": request.user.username_jp,
        "display_name": request.user.display_name,
    }
    if request.method == "POST":
        if form.is_valid():
            form.save(user=request.user)
            return render(request, "done.html", {"text": "プロフィールの変更を行いました"})
    else:
        form = ProfileEditForm(initial=userdata)

    return render(request, "user/edit_profile.html", {"form": form})


@login_required
def add_two_auth(request):
    error = None
    initial_check = TOTPDevice.objects.check_max_totp_device(user=request.user)
    secret = TOTPDevice.objects.generate_secret()
    form = TwoAuthForm()
    buffer = BytesIO()
    qrcode.make(secret.get("url")).save(buffer)
    qr = base64.b64encode(buffer.getvalue()).decode().replace("'", "")
    if request.method == "POST":
        id = request.POST.get("id", 0)
        if id == "submit" and initial_check:
            form = TwoAuthForm(request.POST)
            otp_secret = request.POST.get("secret")
            if form.is_valid():
                code = form.cleaned_data["code"]
                verify_code = pyotp.TOTP(otp_secret).verify(code)
                if verify_code:
                    TOTPDevice.objects.create_secret(
                        user=request.user, title=form.cleaned_data["title"], otp_secret=otp_secret
                    )
                    return redirect("custom_auth:list_two_auth")
                else:
                    error = "コードが一致しません"
            else:
                error = "request error"
        else:
            error = "request error"

    context = {
        "initial_check": initial_check,
        "secret": secret.get("secret"),
        "url": secret.get("url"),
        "qr": qr,
        "form": form,
        "error": error,
    }

    return render(request, "user/two_auth/add.html", context)


@login_required
def list_two_auth(request):
    if request.method == "POST":
        id = request.POST.get("id", 0)
        device_id = int(request.POST.get("device_id", 0))
        if id == "delete":
            TOTPDevice.objects.remove(id=device_id, user=request.user)
    context = {"devices": TOTPDevice.objects.list(user=request.user)}
    return render(request, "user/two_auth/list.html", context)


@login_required
def list_groups(request):
    groups = []
    for group in request.user.groups.all():
        groups.append(
            {
                "data": group,
                "administrator": group.usergroup_set.filter(user=request.user,
                                                            is_admin=True).exists(),
            }
        )

    context = {"groups": groups}
    return render(request, "group/index.html", context)


@login_required
def list_group(request, group_id: int):
    group = request.user.groups.get(id=group_id)
    groups = [
        {
            "data": group,
            "administrator": group.usergroup_set.filter(user=request.user, is_admin=True).exists(),
        }
    ]

    context = {"groups": groups}
    return render(request, "group/index.html", context)


@login_required
def add_group(request):
    error = None
    form = GroupAddForm(data=request.POST or None)
    if not request.user.allow_group_add:
        error = "グループの新規登録が申請不可能です"
    elif request.method == "POST":
        if form.is_valid():
            try:
                form.create_group(user_id=request.user.id)
                return render(request, "done.html", {"text": "登録・変更が完了しました"})
            except ValueError as err:
                error = err
    context = {"form": form, "error": error}
    return render(request, "group/add.html", context)


@login_required
def edit_group(request, group_id: int):
    user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
    if not user_group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})
    form = GroupForm(request.POST or None, instance=user_group.group,
                     editable=not user_group.is_admin)
    if request.method == "POST" and user_group.is_admin and user_group.group.is_pass:
        if form.is_valid():
            form.save()
            return render(request, "done.html", {"text": "登録・変更が完了しました"})

    context = {"form": form, "group": user_group.group, "is_administrator": user_group.is_admin}
    return render(request, "group/edit.html", context)


@login_required
def group_permission(request, group_id: int):
    error = None
    user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
    if not user_group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})
    permissions = user_group.group.usergroup_set.all()
    if request.method == "POST" and user_group.is_admin and user_group.group.is_pass:
        permission_id = int(request.POST.get("id", 0))
        is_exists = user_group.group.usergroup_set.filter(id=permission_id).exists()
        if not is_exists:
            error = "変更権限がありません"
        else:
            try:
                user_group = UserGroup.objects.get(id=permission_id)
                if "no_admin" in request.POST:
                    user_group.is_admin = False
                    user_group.save()
                elif "admin" in request.POST:
                    user_group.is_admin = True
                    user_group.save()
                return redirect(reverse("custom_auth_group:permission", args=[group_id]))
            except Exception:
                error = "アップデート処理でエラーが発生しました"

    context = {
        "group": user_group.group,
        "permissions": permissions,
        "is_administrator": user_group.is_admin,
        "error": error,
    }
    return render(request, "group/edit_permission.html", context)


class PasswordReset(PasswordResetView):
    subject_template_name = "mail/password_reset/subject.txt"
    email_template_name = "mail/password_reset/message.txt"
    template_name = "forget/index.html"
    form_class = ForgetForm
    success_url = reverse_lazy("password_reset_done")


class PasswordResetDone(PasswordResetDoneView):
    template_name = "forget/done.html"


class PasswordResetConfirm(PasswordResetConfirmView):
    form_class = NewSetPasswordForm
    success_url = reverse_lazy("password_reset_complete")
    template_name = "forget/confirm.html"


class PasswordResetComplete(PasswordResetCompleteView):
    template_name = "forget/complete.html"


def activate_user(request, activate_token):
    message = "ユーザーのアクティベーションが完了しました"
    try:
        UserActivateToken.objects.activate_user_by_token(activate_token)
    except ValueError as error:
        message = error
    except Exception:
        message = "エラーが発生しました。管理者に問い合わせてください"
    return render(request, "activate/index.html", {"message": message})
