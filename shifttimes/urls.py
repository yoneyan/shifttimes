"""
URL configuration for shifttimes project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/4.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.conf.urls.static import static
from django.urls import path, include

from custom_auth.views import activate_user
from custom_auth.billing_views import stripe_webhook
from shifttimes import views

urlpatterns = [
    path("login/", views.login, name="login"),
    path("logout/", views.logout, name="logout"),
    path("", views.index, name="index"),
    path("forget/", include("custom_auth.forget_urls")),
    path("notice/", include("notice.urls")),
    path("shift/", include("shift.urls")),
    path("activate/<uuid:activate_token>/", activate_user, name="activate_user"),
    path("profile/", include("custom_auth.urls")),
    path("line/", include("custom_auth.line_urls")),
    path("group/", include("custom_auth.group_urls")),
    path('admin/', admin.site.urls),
]

# オンプレミスモードでは課金機能ごと無効になるので Webhook も受け付けない
if settings.BILLING_ENABLED:
    urlpatterns += [path("stripe/webhook/", stripe_webhook, name="stripe_webhook")]

urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
# DEBUG を settings.py 読み込み後に立てる設定モジュール（develop_settings など）では
# debug_toolbar が INSTALLED_APPS に入らないため、両方揃っている時だけ読み込む
if settings.DEBUG and "debug_toolbar" in settings.INSTALLED_APPS:
    import debug_toolbar

    urlpatterns += [path('__debug__/', include(debug_toolbar.urls))]
