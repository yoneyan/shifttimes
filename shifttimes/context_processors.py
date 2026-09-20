from django.conf import settings


def site(request):
    """サイト全体の表示切り替えに使うフラグ。

    課金導線は billing_enabled が False のときテンプレート側ごと落とす。
    URL 自体も登録されなくなるため、if を外すと NoReverseMatch になる。
    """
    return {
        "site_name": settings.SITE_NAME,
        "onpremise_mode": settings.ONPREMISE_MODE,
        "billing_enabled": settings.BILLING_ENABLED,
    }
