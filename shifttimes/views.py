from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as user_logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from custom_auth import two_factor
from custom_auth.form import AuthTOTP
from notice.models import Notice
from shift.models import ShiftEntry
from shifttimes.form import LoginForm

TOTP_EXPIRED_MESSAGE = "2段階認証の有効期限が切れました。もう一度ログインしてください。"
TOTP_LOCKED_MESSAGE = "認証コードの誤りが続いたため、もう一度ログインからやり直してください。"


def index(request):
    if not request.user.is_authenticated:
        # オンプレミスでは機能紹介も料金表も不要なので、サイト名だけの入口を出す
        if settings.ONPREMISE_MODE:
            return render(request, "landing_onpremise.html", {})
        return render(request, "landing.html", {})

    user = request.user
    today = timezone.now().date()

    memberships = list(user.usergroup_set.select_related("group").all())
    admin_group_ids = {membership.group_id for membership in memberships if membership.is_admin}

    group_cards = [
        {"group": membership.group, "is_admin": membership.group_id in admin_group_ids}
        for membership in memberships
    ]

    total_draft_count = ShiftEntry.objects.filter(
        user=user, group__usergroup__user=user, work_date__gte=today, is_draft=True,
    ).count()

    context = {
        "group_cards": group_cards,
        "has_admin_groups": bool(admin_group_ids),
        "total_draft_count": total_draft_count,
        "notices": Notice.objects.get_notice()[:3],
    }
    return render(request, "index.html", context)


def login(request):
    if request.method == 'POST':
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                # TOTP デバイスを登録しているユーザは、ここではまだログインさせない
                if two_factor.login_or_start_totp(request, user, backend=user.backend):
                    return redirect("/")
                return redirect("login_totp")

    else:
        form = LoginForm()
    context = {'form': form}
    return render(request, "login.html", context)


def login_totp(request):
    """パスワード（または LINE）確認後の 2 段階認証コード入力"""
    user = two_factor.get_pending_user(request)
    if user is None:
        messages.error(request, TOTP_EXPIRED_MESSAGE)
        return redirect("login")

    error = None
    if request.method == "POST":
        form = AuthTOTP(request.POST)
        if form.is_valid():
            if two_factor.verify_pending(request, user, form.cleaned_data["token"]):
                return redirect(settings.LOGIN_REDIRECT_URL)
            if two_factor.get_pending_user(request) is None:
                messages.error(request, TOTP_LOCKED_MESSAGE)
                return redirect("login")
            error = "認証コードが一致しません"
    else:
        form = AuthTOTP()
    return render(request, "login_totp.html", {"form": form, "error": error})


@login_required
def logout(request):
    user_logout(request)
    context = {}
    return render(request, "logout.html", context)
