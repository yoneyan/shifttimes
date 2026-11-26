import datetime
import importlib
import os
from contextlib import contextmanager
from unittest import mock, skipUnless

import pyotp
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, clear_url_caches, reverse

from custom_auth import two_factor
from custom_auth.models import Group, TOTPDevice, TwoFactorChallenge, UserGroup
from shifttimes import notify

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

    onpremise_settings = {name: getattr(load_settings("true"), name) for name in ONPREMISE_SETTING_NAMES}
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


class TwoFactorLoginTests(TestCase):
    """TOTP を登録したユーザはパスワードだけではログインできない"""

    password = "correct-horse-battery"

    def setUp(self):
        self.user = get_user_model().objects.create(
            username="totp-user",
            username_jp="二要素",
            email="totp@example.com",
            is_active=True,
        )
        self.user.set_password(self.password)
        self.user.save()
        self.secret = pyotp.random_base32()
        # コードの生成と照合で同じ時刻を使い、30 秒の時間枠の切り替わりでテストが揺れないようにする
        self.now = datetime.datetime(2026, 1, 15, 12, 0, 10)
        patcher = mock.patch("django.utils.timezone.now", return_value=self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _code(self, secret=None):
        return pyotp.TOTP(secret or self.secret).at(self.now)

    def _register_device(self):
        TOTPDevice.objects.create_secret(user=self.user, title="phone", otp_secret=self.secret)

    def _login(self):
        return self.client.post(reverse("login"), {"username": "totp-user", "password": self.password})

    def test_user_without_device_logs_in_with_password(self):
        response = self._login()

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_password_alone_does_not_log_in_when_device_registered(self):
        self._register_device()

        response = self._login()

        self.assertRedirects(response, reverse("login_verify"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        # 確認待ちのままでは認証が必要なページに入れない
        response = self.client.get(reverse("custom_auth:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_valid_code_completes_login(self):
        self._register_device()
        self._login()

        response = self.client.post(reverse("login_verify"), {"token": self._code()})

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))
        self.assertNotIn(two_factor.SESSION_CHALLENGE_ID, self.client.session)
        self.assertFalse(TwoFactorChallenge.objects.exists())

    def test_wrong_code_is_rejected(self):
        self._register_device()
        self._login()

        response = self.client.post(reverse("login_verify"), {"token": "000000"})

        if self._code() == "000000":
            self.skipTest("偶然 000000 が正しいコードだった")
        self.assertContains(response, "認証コードが一致しません")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_wrong_codes_restart_from_password(self):
        self._register_device()
        self._login()
        wrong = "%06d" % ((int(self._code()) + 500000) % 1000000)

        for _ in range(two_factor.MAX_ATTEMPTS):
            response = self.client.post(reverse("login_verify"), {"token": wrong})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        # 上限後は正しいコードでもログインできない
        response = self.client.post(reverse("login_verify"), {"token": self._code()})
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_pending_login_expires(self):
        self._register_device()
        self._login()
        session = self.client.session
        session[two_factor.SESSION_STARTED_AT] -= two_factor.TIMEOUT_SECONDS + 1
        session.save()

        response = self.client.post(reverse("login_verify"), {"token": self._code()})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_code_cannot_be_replayed_in_the_same_time_step(self):
        self._register_device()
        code = self._code()
        self._login()
        self.client.post(reverse("login_verify"), {"token": code})
        self.assertIn("_auth_user_id", self.client.session)

        # 盗み見た同じコードで別のセッションからログインしようとしても通らない
        other = self.client_class()
        other.post(reverse("login"), {"username": "totp-user", "password": self.password})
        response = other.post(reverse("login_verify"), {"token": code})

        self.assertContains(response, "認証コードが一致しません")
        self.assertNotIn("_auth_user_id", other.session)

    def test_inactive_device_is_not_accepted(self):
        active_secret = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.user, title="new", secret=active_secret, is_active=True)
        TOTPDevice.objects.create(user=self.user, title="lost", secret=self.secret, is_active=False)
        self._login()

        response = self.client.post(reverse("login_verify"), {"token": self._code()})

        if self._code(active_secret) == self._code():
            self.skipTest("偶然 2 つのデバイスのコードが一致した")
        self.assertContains(response, "認証コードが一致しません")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_duplicate_device_cannot_be_created(self):
        self._register_device()

        with self.assertRaises(IntegrityError), transaction.atomic():
            TOTPDevice.objects.create(user=self.user, title="dup", secret=self.secret, is_active=True)

    def test_attempt_limit_is_counted_in_the_database(self):
        """並列送信でセッションの値が上書きされても、DB の試行回数で上限を判定する"""
        self._register_device()
        self._login()
        TwoFactorChallenge.objects.update(attempts=two_factor.MAX_ATTEMPTS - 1)
        wrong = "%06d" % ((int(self._code()) + 500000) % 1000000)

        response = self.client.post(reverse("login_verify"), {"token": wrong})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertFalse(TwoFactorChallenge.objects.exists())

    def test_exhausted_challenge_rejects_even_the_correct_code(self):
        self._register_device()
        self._login()
        TwoFactorChallenge.objects.update(attempts=two_factor.MAX_ATTEMPTS)

        response = self.client.post(reverse("login_verify"), {"token": self._code()})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_blank_code_shows_an_error(self):
        self._register_device()
        self._login()

        response = self.client.post(reverse("login_verify"), {"token": ""})

        self.assertContains(response, "6桁の認証コードを入力してください")

    def test_duplicate_registration_is_ignored(self):
        self._register_device()
        self._register_device()

        self.assertEqual(TOTPDevice.objects.filter(user=self.user).count(), 1)

    def test_pending_login_is_discarded_after_password_change(self):
        self._register_device()
        self._login()
        self.user.set_password("changed-password-123")
        self.user.save()

        response = self.client.post(reverse("login_verify"), {"token": self._code()})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_admin_login_redirects_to_two_factor_login(self):
        self.user.is_staff = True
        self.user.save()
        self._register_device()

        response = self.client.post("/admin/login/", {"username": "totp-user", "password": self.password})

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_verify_page_without_password_step_redirects_to_login(self):
        response = self.client.get(reverse("login_verify"))

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)


class LogoutTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(
            username="leaver", username_jp="退出", email="leaver@example.com", is_active=True
        )
        self.client.force_login(self.user)

    def test_get_does_not_log_out(self):
        response = self.client.get(reverse("logout"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ログアウトしますか")
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_post_logs_out(self):
        response = self.client.post(reverse("logout"))

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class AuditNotifyMaskingTests(TestCase):
    def test_password_hash_is_not_sent_to_slack(self):
        user = get_user_model()(username="masked", username_jp="伏字", email="masked@example.com")
        user.set_password("secret-password")
        client = mock.Mock()

        with mock.patch("shifttimes.notify._get_webhook_client", return_value=client):
            notify.notify_insert_db(model_name="User", instance=user)

        text = client.send.call_args.kwargs["attachments"][0]["text"]
        self.assertNotIn(user.password, text)
        self.assertIn(notify._MASKED_VALUE, text)
