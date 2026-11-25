"""ログイン時の 2 段階認証（TOTP）

ID・パスワード（または LINE）での本人確認が済んだ時点ではまだログインさせず、
ユーザ ID をセッションに一時的に置いて TOTP 入力画面へ送る。
コードを検証できたところで初めて ``login()`` する。
"""
import time

from django.contrib.auth import get_user_model
from django.contrib.auth import login as user_login

from custom_auth.models import TOTPDevice

SESSION_USER_ID = "two_factor_user_id"
SESSION_BACKEND = "two_factor_backend"
SESSION_STARTED_AT = "two_factor_started_at"
SESSION_FAILURES = "two_factor_failures"

# パスワード確認から TOTP 入力までに許す時間（秒）
PENDING_TTL_SECONDS = 5 * 60
# 6 桁コードの総当たりを防ぐため、失敗がこの回数に達したら最初からやり直させる
MAX_FAILURES = 5

_SESSION_KEYS = (SESSION_USER_ID, SESSION_BACKEND, SESSION_STARTED_AT, SESSION_FAILURES)


def requires_totp(user):
    return TOTPDevice.objects.filter(user=user, is_active=True).exists()


def login_or_start_totp(request, user, backend):
    """TOTP が必要なら保留状態にして False、不要ならログインして True を返す"""
    clear_pending(request)
    if requires_totp(user):
        request.session[SESSION_USER_ID] = user.pk
        request.session[SESSION_BACKEND] = backend
        request.session[SESSION_STARTED_AT] = int(time.time())
        request.session[SESSION_FAILURES] = 0
        return False
    user_login(request, user, backend=backend)
    return True


def clear_pending(request):
    for key in _SESSION_KEYS:
        request.session.pop(key, None)


def get_pending_user(request):
    """保留中のユーザを返す。期限切れ・無効なら保留を破棄して None"""
    user_id = request.session.get(SESSION_USER_ID)
    started_at = request.session.get(SESSION_STARTED_AT)
    if user_id is None or started_at is None:
        return None
    if time.time() - started_at > PENDING_TTL_SECONDS:
        clear_pending(request)
        return None
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None:
        clear_pending(request)
    return user


def verify_pending(request, user, code):
    """コードを検証する。成功したらログインして True。

    失敗が MAX_FAILURES に達したら保留を破棄する（呼び出し側は get_pending_user で検知できる）。
    """
    if TOTPDevice.objects.check_totp(user=user, code=code):
        backend = request.session.get(SESSION_BACKEND)
        clear_pending(request)
        user_login(request, user, backend=backend)
        return True

    failures = request.session.get(SESSION_FAILURES, 0) + 1
    if failures >= MAX_FAILURES:
        clear_pending(request)
    else:
        request.session[SESSION_FAILURES] = failures
    return False
