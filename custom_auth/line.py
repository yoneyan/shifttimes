"""LINE ログイン（OAuth 2.0 / OpenID Connect）のクライアント

チャネル ID とチャネルシークレットが両方設定されているときだけ有効になる。
通信は標準ライブラリの ``urllib`` で行い、依存パッケージは増やさない。

メールアドレスは取得しない（LINE 側で申請と審査が必要なため）。取得するのは
ID トークンの ``sub``（LINE のユーザ ID）と表示名・アイコンだけで、
アカウントの特定は既存ユーザとの連携レコード（``LineAccount``）で行う。

ID トークンの検証は LINE の ``/oauth2/v2.1/verify`` に任せる。署名・``iss``・
``aud``・``exp``・``nonce`` をまとめて検証してくれるので、こちら側で JWT を
解く必要がない。

失敗はすべて :class:`LineLoginError` にまとめる。例外のメッセージはそのまま
画面に出すため日本語で、LINE の応答本文はログにだけ残す。
"""
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from django.conf import settings
from django.urls import reverse

logger = logging.getLogger(__name__)

AUTHORIZATION_URL = "https://access.line.me/oauth2/v2.1/authorize"
TOKEN_URL = "https://api.line.me/oauth2/v2.1/token"
VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"

# profile: 表示名とアイコン / openid: ID トークン（sub = LINE のユーザ ID）
SCOPE = "profile openid"

# LINE の応答待ちで画面が固まらないよう短めに区切る
REQUEST_TIMEOUT_SECONDS = 10

COMMUNICATION_ERROR_MESSAGE = "LINEとの通信に失敗しました。時間をおいて再度お試しください。"


class LineLoginError(Exception):
    """画面にそのまま出せる日本語メッセージを持つ例外"""


def is_enabled():
    """LINE ログインが使える設定になっているか"""
    return bool(settings.LINE_LOGIN_CHANNEL_ID and settings.LINE_LOGIN_CHANNEL_SECRET)


def callback_url():
    """LINE Developers のコールバック URL に登録する絶対 URL"""
    return f'{settings.SITE_URL.rstrip("/")}{reverse("custom_auth_line:callback")}'


def generate_token():
    """state / nonce に使う推測不能な文字列"""
    return secrets.token_urlsafe(32)


def build_authorization_url(state, nonce):
    """LINE の同意画面へ送るための URL を組み立てる"""
    params = {
        "response_type": "code",
        "client_id": settings.LINE_LOGIN_CHANNEL_ID,
        "redirect_uri": callback_url(),
        "state": state,
        "scope": SCOPE,
        "nonce": nonce,
    }
    return f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(params)}"


def _post(url, data):
    """LINE の API を叩いて JSON を返す。失敗は LineLoginError にまとめる"""
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        # 本文には invalid_grant などの理由が入る。利用者には見せずログに残す
        body = error.read().decode("utf-8", errors="replace")
        logger.warning("LINE API error: %s status=%s body=%s", url, error.code, body)
        raise LineLoginError(COMMUNICATION_ERROR_MESSAGE) from error
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        logger.warning("LINE API request failed: %s (%s)", url, error)
        raise LineLoginError(COMMUNICATION_ERROR_MESSAGE) from error


def fetch_token(code):
    """認可コードをアクセストークン・ID トークンに交換する"""
    token = _post(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": callback_url(),
            "client_id": settings.LINE_LOGIN_CHANNEL_ID,
            "client_secret": settings.LINE_LOGIN_CHANNEL_SECRET,
        },
    )
    if not token.get("id_token"):
        logger.warning("LINE token response has no id_token: keys=%s", sorted(token))
        raise LineLoginError(COMMUNICATION_ERROR_MESSAGE)
    return token


def verify_id_token(id_token, nonce):
    """ID トークンを LINE に検証してもらい、ペイロードを返す

    署名・iss・aud・exp と nonce の一致は LINE 側で確認される。
    """
    payload = _post(
        VERIFY_URL,
        {
            "id_token": id_token,
            "client_id": settings.LINE_LOGIN_CHANNEL_ID,
            "nonce": nonce,
        },
    )
    if not payload.get("sub"):
        logger.warning("LINE verify response has no sub: keys=%s", sorted(payload))
        raise LineLoginError(COMMUNICATION_ERROR_MESSAGE)
    return payload


def fetch_profile(code, nonce):
    """認可コードから LINE のユーザ情報を取り出す

    戻り値は ``{"line_user_id", "display_name", "picture_url"}``。
    """
    token = fetch_token(code)
    payload = verify_id_token(token["id_token"], nonce)
    return {
        "line_user_id": payload["sub"],
        "display_name": payload.get("name") or "",
        "picture_url": payload.get("picture") or "",
    }
