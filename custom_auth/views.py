import base64
from io import BytesIO

import pyotp
import qrcode
import qrcode.image.svg
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
    SignUpForm,
    TwoAuthForm, ForgetForm, NewSetPasswordForm,
)
from custom_auth.models import UserGroup, User
from custom_auth.models import TOTPDevice, UserActivateToken
from shift.models import AttendanceSetting, SlackNotificationSetting


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
    # PNG 生成は Pillow が必要なため、追加依存のない SVG で描画する
    buffer = BytesIO()
    qrcode.make(secret.get("url"), image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    qr = base64.b64encode(buffer.getvalue()).decode()
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
def group_members(request, group_id: int):
    user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
    if not user_group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    memberships = (
        user_group.group.usergroup_set.select_related("user")
        .order_by("-is_admin", "user__username_jp", "user__username")
    )
    members = [
        {
            "user": membership.user,
            "is_admin": membership.is_admin,
            "created_at": membership.created_at,
            "is_me": membership.user_id == request.user.id,
        }
        for membership in memberships
    ]

    context = {
        "group": user_group.group,
        "members": members,
        "member_count": len(members),
        "admin_count": sum(1 for member in members if member["is_admin"]),
        "is_administrator": user_group.is_admin,
        "max_members": user_group.group.max_members,
        "current_plan": user_group.group.plan,
    }
    return render(request, "group/members.html", context)


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
    if request.method == "POST" and user_group.is_admin:
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
    if request.method == "POST" and user_group.is_admin:
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


@login_required
def group_admin_home(request, group_id: int):
    user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
    if not user_group or not user_group.is_admin:
        return render(request, "error.html", {"text": "このグループの管理者権限がありません"})

    group = user_group.group
    context = {
        "group": group,
        "member_count": group.member_count,
        "max_members": group.max_members,
        "current_plan": group.plan,
        "is_free_granted": group.is_free_granted,
        "can_add_member": group.can_add_member(),
        "attendance_setting": AttendanceSetting.objects.filter(group=group).first(),
        "slack_setting": SlackNotificationSetting.objects.filter(group=group).first(),
    }
    return render(request, "group/admin.html", context)


def _register_user_to_group(request, group_id: int, is_admin_role: bool):
    user_group = request.user.usergroup_set.filter(group_id=group_id, user=request.user).first()
    if not user_group or not user_group.is_admin:
        return render(request, "error.html", {"text": "このグループの管理者権限がありません"})

    group = user_group.group
    # プランの人数上限に達していたら登録させない
    limit_reached = not group.can_add_member()
    limit_message = None
    if limit_reached:
        limit_message = (
            f"メンバー数が{group.plan['name']}プランの上限({group.max_members}名)に達しています。"
            "プランをアップグレードするか、メンバーを減らしてください。"
        )

    error = None
    form = SignUpForm(data=request.POST or None)
    if request.method == "POST" and limit_reached:
        error = limit_message
    elif request.method == "POST" and form.is_valid():
        duplicated = (
            User.objects.filter(username=form.cleaned_data["username"]).exists()
            or User.objects.filter(username_jp=form.cleaned_data["username_jp"]).exists()
            or User.objects.filter(email=form.cleaned_data["email"]).exists()
        )
        if duplicated:
            error = "同じユーザ名またはメールアドレスがすでに登録されています"
        else:
            new_user = form.create_user()
            UserGroup.objects.create(user=new_user, group=group, is_admin=is_admin_role)
            return render(request, "done.html", {"text": "ユーザを登録しました。本人確認用のメールを送信しました。"})

    context = {
        "form": form,
        "error": error,
        "group": group,
        "is_admin_register": is_admin_role,
        "limit_reached": limit_reached,
        "limit_message": limit_message,
        "member_count": group.member_count,
        "max_members": group.max_members,
        "current_plan": group.plan,
    }
    return render(request, "group/register.html", context)


@login_required
def register_member(request, group_id: int):
    return _register_user_to_group(request, group_id, is_admin_role=False)


@login_required
def register_admin(request, group_id: int):
    return _register_user_to_group(request, group_id, is_admin_role=True)


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
