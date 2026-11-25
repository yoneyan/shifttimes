import time
from unittest import mock

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from custom_auth import line, two_factor
from custom_auth.form import AuthTOTP
from custom_auth.models import LineAccount, TOTPDevice

PASSWORD = "correct-horse-battery-staple"


def create_user(username="alice", **extra):
    user = get_user_model().objects.create(
        username=username,
        username_jp=f"{username}_jp",
        email=f"{username}@example.com",
        is_active=True,
        **extra,
    )
    user.set_password(PASSWORD)
    user.save()
    return user


class PasswordLoginTwoFactorTests(TestCase):
    def setUp(self):
        self.user = create_user()
        self.secret = pyotp.random_base32()

    def _add_device(self, is_active=True):
        return TOTPDevice.objects.create(user=self.user, title="phone", secret=self.secret, is_active=is_active)

    def _login(self):
        return self.client.post(reverse("login"), {"username": "alice", "password": PASSWORD})

    def _submit_code(self, code):
        return self.client.post(reverse("login_totp"), {"token": code})

    def _wrong_code(self):
        return "000000" if pyotp.TOTP(self.secret).now() != "000000" else "111111"

    def test_user_without_device_logs_in_directly(self):
        response = self._login()

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_inactive_device_does_not_require_totp(self):
        self._add_device(is_active=False)

        response = self._login()

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_wrong_password_does_not_reach_totp(self):
        self._add_device()

        response = self.client.post(reverse("login"), {"username": "alice", "password": "nope"})

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(two_factor.SESSION_USER_ID, self.client.session)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_user_with_device_is_not_logged_in_after_password(self):
        self._add_device()

        response = self._login()

        self.assertRedirects(response, reverse("login_totp"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[two_factor.SESSION_USER_ID], self.user.pk)
        # 保留中はログイン必須の画面に入れない
        profile = self.client.get(reverse("custom_auth:index"))
        self.assertEqual(profile.status_code, 302)
        self.assertIn(reverse("login"), profile["Location"])

    def test_totp_page_shows_code_field(self):
        self._add_device()
        self._login()

        response = self.client.get(reverse("login_totp"))

        self.assertContains(response, 'name="token"')

    def test_correct_code_logs_in(self):
        self._add_device()
        self._login()

        response = self._submit_code(pyotp.TOTP(self.secret).now())

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))
        self.assertNotIn(two_factor.SESSION_USER_ID, self.client.session)

    def test_code_from_any_registered_device_is_accepted(self):
        self._add_device()
        other_secret = pyotp.random_base32()
        TOTPDevice.objects.create(user=self.user, title="tablet", secret=other_secret, is_active=True)
        self._login()

        response = self._submit_code(pyotp.TOTP(other_secret).now())

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_code_from_another_users_device_is_rejected(self):
        self._add_device()
        other = create_user("bob")
        other_secret = pyotp.random_base32()
        TOTPDevice.objects.create(user=other, title="phone", secret=other_secret, is_active=True)
        self._login()

        response = self._submit_code(pyotp.TOTP(other_secret).now())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_code_is_rejected(self):
        self._add_device()
        self._login()

        response = self._submit_code(self._wrong_code())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "認証コードが一致しません")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(self.client.session[two_factor.SESSION_USER_ID], self.user.pk)

    def test_malformed_code_is_rejected(self):
        self._add_device()
        self._login()

        response = self._submit_code("abc")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_too_many_failures_restart_login(self):
        self._add_device()
        self._login()
        wrong = self._wrong_code()

        for _ in range(two_factor.MAX_FAILURES - 1):
            self.assertEqual(self._submit_code(wrong).status_code, 200)
        response = self._submit_code(wrong)

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn(two_factor.SESSION_USER_ID, self.client.session)
        # 正しいコードでも、もうログインできない
        response = self._submit_code(pyotp.TOTP(self.secret).now())
        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_expired_pending_login_is_rejected(self):
        self._add_device()
        self._login()
        later = time.time() + two_factor.PENDING_TTL_SECONDS + 1

        with mock.patch("custom_auth.two_factor.time.time", return_value=later):
            response = self._submit_code(pyotp.TOTP(self.secret).now())

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_totp_page_without_pending_login_redirects(self):
        response = self.client.get(reverse("login_totp"))

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)

    def test_user_deactivated_while_pending_cannot_log_in(self):
        self._add_device()
        self._login()
        self.user.is_active = False
        self.user.save()

        response = self._submit_code(pyotp.TOTP(self.secret).now())

        self.assertRedirects(response, reverse("login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)


class AuthTOTPFormTests(TestCase):
    def test_leading_zero_is_kept(self):
        form = AuthTOTP(data={"token": "012345"})

        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data["token"], "012345")

    def test_non_six_digit_values_are_invalid(self):
        for value in ("12345", "1234567", "12a456", ""):
            with self.subTest(value=value):
                self.assertFalse(AuthTOTP(data={"token": value}).is_valid())


@override_settings(
    LINE_LOGIN_CHANNEL_ID="1234567890",
    LINE_LOGIN_CHANNEL_SECRET="channel-secret",
    SITE_URL="https://example.com",
)
class LineLoginTwoFactorTests(TestCase):
    def setUp(self):
        self.user = create_user("liner")
        self.secret = pyotp.random_base32()
        LineAccount.objects.create(user=self.user, line_user_id="U0001")

    def _line_login(self):
        self.client.get(reverse("custom_auth_line:login"))
        state = self.client.session["line_oauth_state"]
        with mock.patch.object(line, "fetch_profile", return_value={
            "line_user_id": "U0001", "display_name": "", "picture_url": "",
        }):
            return self.client.get(reverse("custom_auth_line:callback"), {"code": "c", "state": state})

    def test_line_login_without_device_logs_in_directly(self):
        response = self._line_login()

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))

    def test_line_login_with_device_requires_totp(self):
        TOTPDevice.objects.create(user=self.user, title="phone", secret=self.secret, is_active=True)

        response = self._line_login()

        self.assertRedirects(response, reverse("login_totp"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(reverse("login_totp"), {"token": pyotp.TOTP(self.secret).now()})

        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.user.id))
