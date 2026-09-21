from django.conf import settings

from custom_auth import line


def site(request):
    """サイト全体の表示切り替えに使うフラグ。

    課金導線は billing_enabled が False のときテンプレート側ごと落とす。
    URL 自体も登録されなくなるため、if を外すと NoReverseMatch になる。

    LINE ログインの導線は line_login_enabled で出し分ける。こちらは URL を
    常に登録しているので、if を外しても reverse は通る（ビューが案内を返す）。
    """
    return {
        "site_name": settings.SITE_NAME,
        "onpremise_mode": settings.ONPREMISE_MODE,
        "billing_enabled": settings.BILLING_ENABLED,
        "line_login_enabled": line.is_enabled(),
    }
