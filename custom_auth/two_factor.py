"""ログイン時の 2 要素認証（TOTP）

TOTP デバイスを登録しているユーザは、パスワードや LINE での本人確認が通っても
すぐにはログインさせず、認証コードの入力（``login_verify``）を挟む。

確認待ちの間はセッションにユーザ ID・認証バックエンド・開始時刻・失敗回数だけを持たせ、
認証コードが一致した時点で初めて ``django.contrib.auth.login`` する。
"""

import time

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth import login as user_login
from django.shortcuts import redirect
from django.utils.crypto import constant_time_compare

from custom_auth.models import TOTPDevice

SESSION_USER_ID = "two_factor_user_id"
SESSION_BACKEND = "two_factor_backend"
SESSION_STARTED_AT = "two_factor_started_at"
SESSION_ATTEMPTS = "two_factor_attempts"
# 1 要素目の時点の認証ハッシュ。確認待ちの間にパスワードが変わったら破棄する
SESSION_AUTH_HASH = "two_factor_auth_hash"

# パスワード確認から認証コード入力までの猶予
TIMEOUT_SECONDS = 5 * 60
# 認証コードの総当たりを防ぐため、この回数だけ間違えたらパスワードからやり直させる
MAX_ATTEMPTS = 5

DEFAULT_BACKEND = "django.contrib.auth.backends.ModelBackend"


def is_required(user):
    """ログイン時に認証コードの入力が必要か"""
    return TOTPDevice.objects.active(user).exists()


def clear(request):
    for key in (SESSION_USER_ID, SESSION_BACKEND, SESSION_STARTED_AT, SESSION_ATTEMPTS, SESSION_AUTH_HASH):
        request.session.pop(key, None)


def login_or_verify(request, user, backend=None):
    """本人確認（1 要素目）が済んだユーザをログインさせるか、認証コードの入力へ回す"""
    backend = backend or getattr(user, "backend", None) or DEFAULT_BACKEND
    clear(request)
    if not is_required(user):
        user_login(request, user, backend=backend)
        return redirect(settings.LOGIN_REDIRECT_URL)

    request.session[SESSION_USER_ID] = user.pk
    request.session[SESSION_BACKEND] = backend
    request.session[SESSION_STARTED_AT] = time.time()
    request.session[SESSION_ATTEMPTS] = 0
    request.session[SESSION_AUTH_HASH] = user.get_session_auth_hash()
    return redirect("login_verify")


def pending_user(request):
    """認証コードの入力待ちのユーザ。期限切れ・無効なら確認待ちを破棄して None を返す"""
    user_id = request.session.get(SESSION_USER_ID)
    started_at = request.session.get(SESSION_STARTED_AT)
    if user_id is None or started_at is None:
        return None
    if time.time() - started_at > TIMEOUT_SECONDS:
        clear(request)
        return None
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    auth_hash = request.session.get(SESSION_AUTH_HASH) or ""
    if user is None or not constant_time_compare(auth_hash, user.get_session_auth_hash()):
        # 確認待ちの間にパスワードが変更された場合は、変更前の認証で始めたログインを完了させない
        clear(request)
        return None
    return user


def verify(request, user, code):
    """認証コードを確かめる。一致すればログインさせて True を返す

    失敗回数が上限に達したら確認待ちを破棄する（呼び出し側は pending_user() で判定する）。
    """
    code = (code or "").strip().replace(" ", "")
    if code.isdigit() and len(code) == 6 and TOTPDevice.objects.check_totp(user=user, code=code):
        backend = request.session.get(SESSION_BACKEND) or DEFAULT_BACKEND
        clear(request)
        user_login(request, user, backend=backend)
        return True

    attempts = request.session.get(SESSION_ATTEMPTS, 0) + 1
    if attempts >= MAX_ATTEMPTS:
        clear(request)
    else:
        request.session[SESSION_ATTEMPTS] = attempts
    return False
