"""LINE ログインのビュー

流れは 2 とおりあるが、LINE から戻ってくる先（コールバック）は 1 つなので、
どちらの用途で開始したかをセッションに持たせて分岐する。

* ログイン（``line_login``） — 未ログインの人が LINE でログインする
* 連携（``line_link``） — ログイン済みの人が自分の LINE アカウントを結びつける

連携されていない LINE アカウントでは新規ユーザを作らず、案内画面を返す。
"""
import secrets

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as user_login
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from custom_auth import line
from custom_auth.models import LineAccount

PURPOSE_LOGIN = "login"
PURPOSE_LINK = "link"

SESSION_STATE = "line_oauth_state"
SESSION_NONCE = "line_oauth_nonce"
SESSION_PURPOSE = "line_oauth_purpose"

DISABLED_MESSAGE = "LINEログインは利用できません。管理者に問い合わせてください。"
INVALID_SESSION_MESSAGE = "LINEログインのセッションが無効です。最初からやり直してください。"

# ログインに使う認証バックエンド。LINE 連携は本人確認済みとして扱うので
# authenticate() を経由せず、明示的にバックエンドを指定してログインさせる
AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"


def _clear_session(request):
    for key in (SESSION_STATE, SESSION_NONCE, SESSION_PURPOSE):
        request.session.pop(key, None)


def _start(request, purpose):
    """state と nonce を発行して LINE の同意画面へ送る"""
    if not line.is_enabled():
        return render(request, "error.html", {"text": DISABLED_MESSAGE})

    state = line.generate_token()
    nonce = line.generate_token()
    request.session[SESSION_STATE] = state
    request.session[SESSION_NONCE] = nonce
    request.session[SESSION_PURPOSE] = purpose
    return redirect(line.build_authorization_url(state, nonce))


def line_login(request):
    if request.user.is_authenticated:
        return redirect(settings.LOGIN_REDIRECT_URL)
    return _start(request, PURPOSE_LOGIN)


@login_required
def line_link(request):
    return _start(request, PURPOSE_LINK)


def callback(request):
    """LINE からのリダイレクトを受け取る"""
    if not line.is_enabled():
        return render(request, "error.html", {"text": DISABLED_MESSAGE})

    state = request.session.get(SESSION_STATE)
    nonce = request.session.get(SESSION_NONCE)
    purpose = request.session.get(SESSION_PURPOSE)
    # state は 1 回きり。ここから先は成否にかかわらず再利用させない
    _clear_session(request)

    if request.GET.get("error"):
        # 同意画面でキャンセルされた場合など。理由は LINE から description で来る
        return render(request, "error.html", {"text": "LINEでの認証が中断されました。"})

    code = request.GET.get("code", "")
    received_state = request.GET.get("state", "")
    if not code or not state or not secrets.compare_digest(state, received_state):
        return render(request, "error.html", {"text": INVALID_SESSION_MESSAGE})

    try:
        profile = line.fetch_profile(code, nonce)
    except line.LineLoginError as error:
        return render(request, "error.html", {"text": str(error)})

    if purpose == PURPOSE_LINK:
        return _complete_link(request, profile)
    if purpose == PURPOSE_LOGIN:
        return _complete_login(request, profile)
    return render(request, "error.html", {"text": INVALID_SESSION_MESSAGE})


def _complete_login(request, profile):
    account = (
        LineAccount.objects.select_related("user")
        .filter(line_user_id=profile["line_user_id"])
        .first()
    )
    if not account:
        # 新規ユーザは作らない。先に ID・パスワードでログインして連携してもらう
        return render(request, "user/line_not_linked.html", {})
    if not account.user.is_active:
        return render(request, "error.html", {"text": "アカウントが有効化されていません。"})

    account.display_name = profile["display_name"]
    account.picture_url = profile["picture_url"]
    account.last_login_at = timezone.now()
    account.updated_at = timezone.now()
    account.save()

    user_login(request, account.user, backend=AUTH_BACKEND)
    return redirect(settings.LOGIN_REDIRECT_URL)


def _complete_link(request, profile):
    if not request.user.is_authenticated:
        # 連携の途中でセッションが切れた場合
        return render(request, "error.html", {"text": INVALID_SESSION_MESSAGE})

    taken = (
        LineAccount.objects.filter(line_user_id=profile["line_user_id"])
        .exclude(user=request.user)
        .exists()
    )
    if taken:
        return render(
            request,
            "error.html",
            {"text": "このLINEアカウントは別のユーザに連携済みです。"},
        )

    try:
        LineAccount.objects.update_or_create(
            user=request.user,
            defaults={
                "line_user_id": profile["line_user_id"],
                "display_name": profile["display_name"],
                "picture_url": profile["picture_url"],
                "updated_at": timezone.now(),
            },
        )
    except IntegrityError:
        return render(
            request,
            "error.html",
            {"text": "このLINEアカウントは別のユーザに連携済みです。"},
        )

    messages.success(request, "LINEアカウントを連携しました。")
    return redirect("custom_auth:line_account")


@login_required
def account(request):
    """LINE 連携の状態と、連携・解除の導線"""
    # line_login_enabled はコンテキストプロセッサ(site)から渡る
    context = {"line_account": LineAccount.objects.filter(user=request.user).first()}
    return render(request, "user/line.html", context)


@login_required
@require_POST
def unlink(request):
    deleted, _ = LineAccount.objects.filter(user=request.user).delete()
    if deleted:
        messages.success(request, "LINEアカウントの連携を解除しました。")
    return redirect("custom_auth:line_account")
