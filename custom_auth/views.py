import base64
from io import BytesIO

import pyotp
import qrcode
import qrcode.image.svg
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import (
    PasswordResetCompleteView,
    PasswordResetConfirmView,
    PasswordResetDoneView,
    PasswordResetView,
)
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy

from custom_auth.form import (
    EmailChangeForm,
    ForgetForm,
    GroupAddForm,
    GroupForm,
    MyPasswordChangeForm,
    NewSetPasswordForm,
    ProfileEditForm,
    SignUpForm,
    TwoAuthForm,
)
from custom_auth.models import TOTPDevice, User, UserActivateToken, UserGroup
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
                "administrator": group.usergroup_set.filter(user=request.user, is_admin=True).exists(),
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

    memberships = user_group.group.usergroup_set.select_related("user").order_by(
        "-is_admin", "user__username_jp", "user__username"
    )
    members = [
        {
            "user": membership.user,
            "is_admin": membership.is_admin,
            "created_at": membership.created_at,
            "is_me": membership.user_id == request.user.id,
            "membership_id": membership.id,
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
        "can_leave": _can_leave_group(user_group),
    }
    return render(request, "group/members.html", context)


class MembershipChangeError(Exception):
    """メンバーの削除・権限変更ができないときのエラー（メッセージは画面にそのまま出す）"""


_LAST_ADMIN_MESSAGE = "グループの管理者が 1 人もいなくなるため、変更できません。先に他のメンバーを管理者にしてください"


def _can_leave_group(membership):
    """グループから抜けても管理者が 1 人以上残るか"""
    if not membership.is_admin:
        return True
    return UserGroup.objects.filter(group_id=membership.group_id, is_admin=True).exclude(id=membership.id).exists()


def _lock_memberships(group_id, actor_id=None):
    """グループの所属をすべてロックして返す。actor_id を渡すと、その人が今も管理者かを確かめる

    削除と権限変更が並行して管理者が 0 人になったり、外された・降格された管理者の
    処理中のリクエストが通ったりしないよう、所属の変更は必ずこのロックの中で判定する。
    トランザクションの中で呼ぶこと。
    """
    memberships = list(UserGroup.objects.select_for_update().filter(group_id=group_id).order_by("id"))
    if actor_id is not None and not any(m.user_id == actor_id and m.is_admin for m in memberships):
        raise MembershipChangeError("このグループの管理者権限がありません")
    return memberships


def _find_membership(memberships, membership_id):
    target = next((membership for membership in memberships if membership.id == membership_id), None)
    if target is None:
        raise MembershipChangeError("対象のメンバーが見つかりません")
    return target


def _remove_membership(membership_id, group_id, actor_id=None):
    """メンバーをグループから外す。管理者が 0 人になる場合は MembershipChangeError

    actor_id は管理者として他人を外すときの操作ユーザー（自分で脱退するときは None）。
    ユーザのアカウントと、過去のシフト希望・勤怠は残す（勤怠集計では退会済みとして表示する）。
    """
    with transaction.atomic():
        memberships = _lock_memberships(group_id, actor_id)
        target = _find_membership(memberships, membership_id)
        if target.is_admin and not any(m.is_admin and m.id != target.id for m in memberships):
            raise MembershipChangeError(_LAST_ADMIN_MESSAGE)
        target.delete()
        return target


def _change_admin_role(membership_id, group_id, actor_id, make_admin):
    """管理者権限を付け外しする。自分の権限の変更と、管理者が 0 人になる変更は MembershipChangeError"""
    with transaction.atomic():
        memberships = _lock_memberships(group_id, actor_id)
        target = _find_membership(memberships, membership_id)
        if target.user_id == actor_id:
            raise MembershipChangeError("自分の権限は変更できません")
        if not make_admin and not any(m.is_admin and m.id != target.id for m in memberships):
            raise MembershipChangeError(_LAST_ADMIN_MESSAGE)
        target.is_admin = make_admin
        target.save(update_fields=["is_admin"])
        return target


@login_required
def remove_member(request, group_id: int, membership_id: int):
    """グループ管理者がメンバーをグループから外す（GET は確認画面）"""
    user_group = request.user.usergroup_set.filter(group_id=group_id).select_related("group").first()
    if not user_group or not user_group.is_admin:
        return render(request, "error.html", {"text": "このグループの管理者権限がありません"})

    target = UserGroup.objects.filter(id=membership_id, group_id=group_id).select_related("user").first()
    if target is None:
        return render(request, "error.html", {"text": "対象のメンバーが見つかりません"})
    if target.user_id == request.user.id:
        # 自分を外すのは「脱退」の画面から行う（確認の文言と移動先が違うため）
        return redirect("custom_auth_group:leave", group_id=group_id)

    error = None
    if request.method == "POST":
        try:
            _remove_membership(target.id, group_id, actor_id=request.user.id)
        except MembershipChangeError as err:
            error = str(err)
        else:
            messages.success(request, f"{target.user.get_display_name()} さんをグループから外しました")
            return redirect("custom_auth_group:members", group_id=group_id)

    context = {"group": user_group.group, "target": target, "error": error}
    return render(request, "group/remove_member.html", context)


@login_required
def leave_group(request, group_id: int):
    """自分がグループから脱退する（GET は確認画面）"""
    user_group = request.user.usergroup_set.filter(group_id=group_id).select_related("group").first()
    if not user_group:
        return render(request, "error.html", {"text": "このグループにアクセスする権限がありません"})

    error = None
    if request.method == "POST":
        try:
            _remove_membership(user_group.id, group_id)
        except MembershipChangeError as err:
            error = str(err)
        else:
            messages.success(request, f"{user_group.group.name_jp} から脱退しました")
            return redirect("custom_auth_group:index")

    context = {
        "group": user_group.group,
        "membership": user_group,
        "can_leave": _can_leave_group(user_group),
        "error": error,
    }
    return render(request, "group/leave.html", context)


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
    form = GroupForm(request.POST or None, instance=user_group.group, editable=not user_group.is_admin)
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
    if request.method == "POST" and user_group.is_admin and ("admin" in request.POST or "no_admin" in request.POST):
        try:
            permission_id = int(request.POST.get("id", 0))
            _change_admin_role(permission_id, group_id, request.user.id, make_admin="admin" in request.POST)
        except ValueError:
            error = "変更対象が不正です"
        except MembershipChangeError as err:
            error = str(err)
        else:
            return redirect(reverse("custom_auth_group:permission", args=[group_id]))

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
