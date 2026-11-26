from django.conf import settings
from django.contrib.auth import logout as user_logout
from django.shortcuts import redirect, render
from django.utils import timezone

from custom_auth import two_factor
from custom_auth.form import OTPForm
from notice.models import Notice
from shift.models import ShiftEntry
from shifttimes.form import LoginForm


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
        {"group": membership.group, "is_admin": membership.group_id in admin_group_ids} for membership in memberships
    ]

    total_draft_count = ShiftEntry.objects.filter(
        user=user,
        group__usergroup__user=user,
        work_date__gte=today,
        is_draft=True,
    ).count()

    context = {
        "group_cards": group_cards,
        "has_admin_groups": bool(admin_group_ids),
        "total_draft_count": total_draft_count,
        "notices": Notice.objects.get_notice()[:3],
    }
    return render(request, "index.html", context)


def login(request):
    if request.method == "POST":
        form = LoginForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            if user:
                # TOTP を登録しているユーザは認証コードの入力を挟む
                return two_factor.login_or_verify(request, user)

    else:
        form = LoginForm()
    context = {"form": form}
    return render(request, "login.html", context)


def login_verify(request):
    """ログイン時の 2 要素認証（認証コードの入力）"""
    user = two_factor.pending_user(request)
    if user is None:
        return redirect("login")

    error = None
    form = OTPForm(data=request.POST or None)
    if request.method == "POST" and form.is_valid():
        if two_factor.verify(request, user, form.cleaned_data["token"]):
            return redirect(settings.LOGIN_REDIRECT_URL)
        if two_factor.pending_user(request) is None:
            # 失敗回数の上限に達したので最初からやり直させる
            return redirect("login")
        error = "認証コードが一致しません"

    return render(request, "login_verify.html", {"form": form, "error": error})


def logout(request):
    # GET でのログアウトはリンクや画像を踏ませるだけで強制ログアウトできてしまうため、
    # 実際のログアウトは CSRF トークン付きの POST に限る
    if request.method != "POST":
        if not request.user.is_authenticated:
            return redirect("login")
        return render(request, "logout.html", {"confirm": True})
    user_logout(request)
    return render(request, "logout.html", {})
