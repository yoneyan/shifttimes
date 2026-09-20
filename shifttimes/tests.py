import importlib
import os
from contextlib import contextmanager
from unittest import mock, skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, clear_url_caches, reverse

from custom_auth.models import Group, UserGroup

# ONPREMISE_MODE で settings.py が差し替える設定
ONPREMISE_SETTING_NAMES = (
    "ONPREMISE_MODE",
    "BILLING_ENABLED",
    "FREE_PLAN",
    "STRIPE_PLANS",
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
)


def load_settings(onpremise):
    """settings.py を任意の ONPREMISE_MODE で読み直す。

    読み直しても django.conf.settings は起動時のコピーのままなので、
    現在のテスト実行には影響しない。
    """
    module = importlib.import_module("shifttimes.settings")
    with mock.patch.dict(os.environ, {"ONPREMISE_MODE": onpremise}):
        return importlib.reload(module)


def restore_settings_module():
    """settings.py をプロセス本来の環境変数で読み直す。"""
    importlib.reload(importlib.import_module("shifttimes.settings"))


@contextmanager
def onpremise_mode():
    """ONPREMISE_MODE=true 相当の設定と URLConf に差し替える。

    差し替える値は settings.py を読み直して取り出すので、
    settings.py 側の条件を変えてもこのヘルパは追随する。
    urlpatterns は import 時に組み立てられるため、override_settings だけでは
    課金 URL の有無が切り替わらない。モジュールごと読み直す。
    """
    from custom_auth import group_urls
    from shifttimes import urls as root_urls

    onpremise_settings = {
        name: getattr(load_settings("true"), name) for name in ONPREMISE_SETTING_NAMES
    }
    restore_settings_module()

    def reload_all():
        importlib.reload(group_urls)
        importlib.reload(root_urls)
        clear_url_caches()

    try:
        with override_settings(**onpremise_settings):
            reload_all()
            yield
    finally:
        # override 解除後の値で組み立て直し、後続のテストに漏らさない
        reload_all()


class OnpremiseSettingsTests(TestCase):
    def tearDown(self):
        restore_settings_module()

    def test_onpremise_disables_billing(self):
        module = load_settings("true")

        self.assertTrue(module.ONPREMISE_MODE)
        self.assertFalse(module.BILLING_ENABLED)
        self.assertEqual(module.STRIPE_PLANS, {})
        self.assertEqual(module.STRIPE_SECRET_KEY, "")
        self.assertEqual(module.STRIPE_PUBLISHABLE_KEY, "")
        self.assertEqual(module.STRIPE_WEBHOOK_SECRET, "")
        # オンプレミスは Enterprise プラン扱い（人数無制限）
        self.assertEqual(module.FREE_PLAN, module.UNLIMITED_PLAN)
        self.assertEqual(module.FREE_PLAN["name"], "Enterprise")
        self.assertIsNone(module.FREE_PLAN["max_members"])

    def test_default_keeps_billing(self):
        module = load_settings("false")

        self.assertFalse(module.ONPREMISE_MODE)
        self.assertTrue(module.BILLING_ENABLED)
        self.assertIn("standard", module.STRIPE_PLANS)
        self.assertEqual(module.FREE_PLAN["max_members"], 10)


class LandingPageTests(TestCase):
    @skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
    def test_default_landing_shows_features_and_pricing(self):
        response = self.client.get(reverse("index"))

        self.assertTemplateUsed(response, "landing.html")
        self.assertContains(response, "主な機能")
        self.assertContains(response, "料金プラン")

    @override_settings(ONPREMISE_MODE=True, SITE_NAME="社内シフト管理")
    def test_onpremise_landing_shows_site_name_only(self):
        response = self.client.get(reverse("index"))

        self.assertTemplateUsed(response, "landing_onpremise.html")
        self.assertContains(response, "社内シフト管理")
        self.assertNotContains(response, "主な機能")
        self.assertNotContains(response, "料金プラン")


class OnpremiseBillingDisabledTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin_user = user_model.objects.create(
            username="admin",
            username_jp="管理者",
            email="admin@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)

    def test_billing_urls_are_not_registered(self):
        with onpremise_mode():
            with self.assertRaises(NoReverseMatch):
                reverse("custom_auth_group:billing", args=[self.group.id])
            with self.assertRaises(NoReverseMatch):
                reverse("stripe_webhook")

            self.assertEqual(self.client.get(f"/group/{self.group.id}/billing/").status_code, 404)
            self.assertEqual(self.client.post("/stripe/webhook/").status_code, 404)

    def test_group_admin_page_shows_enterprise_without_billing(self):
        self.client.force_login(self.admin_user)
        with onpremise_mode():
            response = self.client.get(
                reverse("custom_auth_group:admin_home", args=[self.group.id]),
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "請求管理")
        # 課金導線は消えるが、Enterprise プラン扱いであることは表示する
        self.assertContains(response, "Enterprise")

    def test_notice_page_hides_unpaid_warning(self):
        self.client.force_login(self.admin_user)
        with onpremise_mode():
            response = self.client.get(reverse("notice:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "未課金のグループがあります")

    @skipUnless(settings.BILLING_ENABLED, "ONPREMISE_MODE では課金機能が無効なため対象外")
    def test_billing_urls_exist_by_default(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("custom_auth_group:admin_home", args=[self.group.id]))

        self.assertContains(response, "請求管理")
