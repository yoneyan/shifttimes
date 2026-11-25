import re
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

OLD_PASSWORD = "Old-Passw0rd!"
NEW_PASSWORD = "N3w-Secure-Passw0rd!"


@override_settings(DOMAIN_URL="https://shift.example.com", SITE_NAME="テストシフト")
class PasswordResetFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(
            username="worker",
            username_jp="従業員",
            email="worker@example.com",
            is_active=True,
        )
        self.user.set_password(OLD_PASSWORD)
        self.user.save()

    def _request_reset(self, email):
        return self.client.post(reverse("custom_auth_forget:index"), {"email": email})

    def _reset_path_from_mail(self):
        self.assertEqual(len(mail.outbox), 1)
        urls = re.findall(r"https?://\S+", mail.outbox[0].body)
        self.assertEqual(len(urls), 1)
        url = urlsplit(urls[0])
        self.assertEqual(f"{url.scheme}://{url.netloc}", "https://shift.example.com")
        return url.path

    def test_login_page_links_to_password_reset(self):
        response = self.client.get(reverse("login"))

        self.assertContains(response, reverse("custom_auth_forget:index"))

    def test_reset_form_is_displayed(self):
        response = self.client.get(reverse("custom_auth_forget:index"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "forget/index.html")

    def test_full_password_reset_flow(self):
        # メールアドレスを送信すると完了画面へ遷移し、再設定メールが届く
        response = self._request_reset("worker@example.com")
        self.assertRedirects(response, reverse("custom_auth_forget:done"))
        response = self.client.get(reverse("custom_auth_forget:done"))
        self.assertTemplateUsed(response, "forget/done.html")

        self.assertEqual(mail.outbox[0].to, ["worker@example.com"])
        self.assertEqual(mail.outbox[0].subject, "【テストシフト】パスワード再設定のご案内")
        reset_path = self._reset_path_from_mail()

        # メール内のリンクを開くと、トークンを隠した set-password 画面へリダイレクトされる
        response = self.client.get(reset_path, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "forget/confirm.html")
        self.assertTrue(response.context["validlink"])
        set_password_path = response.redirect_chain[-1][0]

        # 新しいパスワードを設定する
        response = self.client.post(
            set_password_path,
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )
        self.assertRedirects(response, reverse("custom_auth_forget:complete"))
        response = self.client.get(reverse("custom_auth_forget:complete"))
        self.assertTemplateUsed(response, "forget/complete.html")

        # 旧パスワードではログインできず、新パスワードでログインできる
        response = self.client.post(reverse("login"), {"username": "worker", "password": OLD_PASSWORD})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

        response = self.client.post(reverse("login"), {"username": "worker", "password": NEW_PASSWORD})
        self.assertRedirects(response, "/", fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_reset_link_cannot_be_reused(self):
        self._request_reset("worker@example.com")
        reset_path = self._reset_path_from_mail()
        response = self.client.get(reset_path, follow=True)
        self.client.post(
            response.redirect_chain[-1][0],
            {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD},
        )

        response = self.client.get(reset_path, follow=True)

        self.assertFalse(response.context["validlink"])

    def test_invalid_token_is_rejected(self):
        self._request_reset("worker@example.com")
        reset_path = self._reset_path_from_mail()
        tampered_path = reset_path.rstrip("/") + "x/"

        response = self.client.get(tampered_path)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["validlink"])

    def test_unknown_email_sends_no_mail_but_shows_done(self):
        response = self._request_reset("nobody@example.com")

        self.assertRedirects(response, reverse("custom_auth_forget:done"))
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_user_receives_no_mail(self):
        self.user.is_active = False
        self.user.save()

        self._request_reset("worker@example.com")

        self.assertEqual(len(mail.outbox), 0)
