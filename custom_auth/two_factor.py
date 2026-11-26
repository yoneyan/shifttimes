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

from custom_auth.models import TOTPDevice, TwoFactorChallenge, TwoFactorThrottle

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

# ユーザ単位の上限。ログインし直しても補充されないよう、この時間内の失敗回数で判定する
USER_MAX_FAILURES = 10
USER_WINDOW_SECONDS = 15 * 60

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


def _reserve_user_attempt(user):
    """ユーザ単位の試行枠を 1 回分確保する。上限に達していれば False

    時間枠のリセットと確保はどちらも条件付き UPDATE なので、並列リクエストでも上限を超えない。
    """
    now = timezone.now()
    TwoFactorThrottle.objects.get_or_create(user=user, defaults={"window_started_at": now})
    window_expired_before = now - datetime.timedelta(seconds=USER_WINDOW_SECONDS)
    TwoFactorThrottle.objects.filter(user=user, window_started_at__lt=window_expired_before).update(
        failures=0, window_started_at=now
    )
    return bool(
        TwoFactorThrottle.objects.filter(user=user, failures__lt=USER_MAX_FAILURES).update(failures=F("failures") + 1)
    )


def is_locked(user):
    """ユーザ単位の試行上限に達していて、時間枠が明けるまで認証コードを受け付けない状態か"""
    window_expired_before = timezone.now() - datetime.timedelta(seconds=USER_WINDOW_SECONDS)
    return TwoFactorThrottle.objects.filter(
        user=user, failures__gte=USER_MAX_FAILURES, window_started_at__gte=window_expired_before
    ).exists()


def verify(request, user, code):
    """認証コードを確かめる。一致すればログインさせて True を返す

    照合の前に、ユーザ単位と確認待ち単位の試行枠を条件付き UPDATE で 1 回分ずつ確保するので、
    並列に送られても、ログインし直しても上限を超えて照合されることはない。確認待ち単位の上限に
    達したら確認待ちを破棄する（呼び出し側は pending_user() / is_locked() で判定する）。
    """
    if not _reserve_user_attempt(user):
        # ユーザ単位の上限に達している間は照合しない（確認待ちの試行枠も消費しない）
        return False

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
        # 本人が正しいコードを入力できたので、ユーザ単位の失敗回数を戻す
        TwoFactorThrottle.objects.filter(user=user).update(failures=0)
        user_login(request, user, backend=backend)
        return True

    if TwoFactorChallenge.objects.filter(pk=challenge_id, attempts__gte=MAX_ATTEMPTS).exists():
        clear(request)
    return False
