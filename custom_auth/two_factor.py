"""ログイン時の 2 要素認証（TOTP）

TOTP デバイスを登録しているユーザは、パスワードや LINE での本人確認が通っても
すぐにはログインさせず、認証コードの入力（``login_verify``）を挟む。

確認待ちのログインは ``TwoFactorChallenge`` の行として DB に持ち、失敗回数もそこで数える。
セッションに持つと並列リクエストで回数が上書きされ、試行上限を超えて総当たりできてしまうため。
セッションには確認待ちの ID・認証バックエンド・開始時刻・認証ハッシュだけを置き、
認証コードが一致した時点で初めて ``django.contrib.auth.login`` する。
"""

import datetime
import time

from django.conf import settings
from django.contrib.auth import login as user_login
from django.db.models import F
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from custom_auth.models import TOTPDevice, TwoFactorChallenge

SESSION_CHALLENGE_ID = "two_factor_challenge_id"
SESSION_BACKEND = "two_factor_backend"
SESSION_STARTED_AT = "two_factor_started_at"
# 1 要素目の時点の認証ハッシュ。確認待ちの間にパスワードが変わったら破棄する
SESSION_AUTH_HASH = "two_factor_auth_hash"
SESSION_KEYS = (SESSION_CHALLENGE_ID, SESSION_BACKEND, SESSION_STARTED_AT, SESSION_AUTH_HASH)

# パスワード確認から認証コード入力までの猶予
TIMEOUT_SECONDS = 5 * 60
# 認証コードの総当たりを防ぐため、この回数だけ間違えたらパスワードからやり直させる
MAX_ATTEMPTS = 5

DEFAULT_BACKEND = "django.contrib.auth.backends.ModelBackend"


def is_required(user):
    """ログイン時に認証コードの入力が必要か"""
    return TOTPDevice.objects.active(user).exists()


def clear(request):
    """確認待ちを破棄する（DB の行とセッションの両方）"""
    challenge_id = request.session.get(SESSION_CHALLENGE_ID)
    if challenge_id is not None:
        TwoFactorChallenge.objects.filter(pk=challenge_id).delete()
    for key in SESSION_KEYS:
        request.session.pop(key, None)


def _delete_expired_challenges():
    """入力されないまま放置された確認待ちを掃除する"""
    expired_before = timezone.now() - datetime.timedelta(seconds=TIMEOUT_SECONDS)
    TwoFactorChallenge.objects.filter(created_at__lt=expired_before).delete()


def login_or_verify(request, user, backend=None):
    """本人確認（1 要素目）が済んだユーザをログインさせるか、認証コードの入力へ回す"""
    backend = backend or getattr(user, "backend", None) or DEFAULT_BACKEND
    clear(request)
    if not is_required(user):
        user_login(request, user, backend=backend)
        return redirect(settings.LOGIN_REDIRECT_URL)

    _delete_expired_challenges()
    challenge = TwoFactorChallenge.objects.create(user=user)
    request.session[SESSION_CHALLENGE_ID] = challenge.pk
    request.session[SESSION_BACKEND] = backend
    request.session[SESSION_STARTED_AT] = time.time()
    request.session[SESSION_AUTH_HASH] = user.get_session_auth_hash()
    return redirect("login_verify")


def pending_user(request):
    """認証コードの入力待ちのユーザ。期限切れ・試行上限・無効なら確認待ちを破棄して None を返す"""
    challenge_id = request.session.get(SESSION_CHALLENGE_ID)
    started_at = request.session.get(SESSION_STARTED_AT)
    if challenge_id is None or started_at is None:
        return None
    if time.time() - started_at > TIMEOUT_SECONDS:
        clear(request)
        return None
    challenge = (
        TwoFactorChallenge.objects.select_related("user")
        .filter(pk=challenge_id, attempts__lt=MAX_ATTEMPTS, user__is_active=True)
        .first()
    )
    if challenge is None:
        clear(request)
        return None
    user = challenge.user
    auth_hash = request.session.get(SESSION_AUTH_HASH) or ""
    if not constant_time_compare(auth_hash, user.get_session_auth_hash()):
        # 確認待ちの間にパスワードが変更された場合は、変更前の認証で始めたログインを完了させない
        clear(request)
        return None
    return user


def verify(request, user, code):
    """認証コードを確かめる。一致すればログインさせて True を返す

    照合の前に試行枠を条件付き UPDATE で 1 回分確保するので、並列に送られても
    上限を超えて照合されることはない。上限に達したら確認待ちを破棄する
    （呼び出し側は pending_user() で判定する）。
    """
    challenge_id = request.session.get(SESSION_CHALLENGE_ID)
    reserved = TwoFactorChallenge.objects.filter(pk=challenge_id, user=user, attempts__lt=MAX_ATTEMPTS).update(
        attempts=F("attempts") + 1
    )
    if not reserved:
        clear(request)
        return False

    code = (code or "").strip().replace(" ", "")
    if code.isdigit() and len(code) == 6 and TOTPDevice.objects.check_totp(user=user, code=code):
        backend = request.session.get(SESSION_BACKEND) or DEFAULT_BACKEND
        clear(request)
        user_login(request, user, backend=backend)
        return True

    if TwoFactorChallenge.objects.filter(pk=challenge_id, attempts__gte=MAX_ATTEMPTS).exists():
        clear(request)
    return False
