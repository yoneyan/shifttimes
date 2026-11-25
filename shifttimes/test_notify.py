from unittest import mock

import pyotp
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from custom_auth.models import TOTPDevice
from shifttimes import notify


def sent_texts(client_class):
    """モック化した WebhookClient に送られた本文をすべて連結して返す"""
    texts = []
    for call in client_class.return_value.send.call_args_list:
        texts.append(call.kwargs.get("text") or "")
        for attachment in call.kwargs.get("attachments", []):
            texts.append(attachment.get("title", ""))
            texts.append(attachment.get("text", ""))
    return "\n".join(texts)


@override_settings(SLACK_WEBHOOK_URL_LOG="https://hooks.slack.com/services/test")
@mock.patch("shifttimes.notify.WebhookClient")
class NotifySensitiveFieldTests(TestCase):
    def _create_user(self):
        user = get_user_model().objects.create(
            username="alice", username_jp="アリス", email="alice@example.com", is_active=True,
        )
        user.set_password("s3cret-password")
        user.save()
        return user

    def test_user_save_does_not_send_password_hash(self, client_class):
        user = self._create_user()

        text = sent_texts(client_class)
        self.assertTrue(client_class.return_value.send.called)
        self.assertIn("alice", text)
        self.assertNotIn(user.password, text)
        self.assertNotIn("password", text)
        self.assertNotIn("パスワード", text)

    def test_insert_notification_omits_totp_secret(self, client_class):
        user = self._create_user()
        client_class.return_value.send.reset_mock()
        secret = pyotp.random_base32()
        device = TOTPDevice.objects.create(user=user, title="phone", secret=secret, is_active=True)

        notify.notify_insert_db(model_name="TOTPDevice", instance=device)

        text = sent_texts(client_class)
        self.assertIn("phone", text)
        self.assertNotIn(secret, text)

    def test_update_notification_omits_password_change(self, client_class):
        user = self._create_user()
        user.display_name = "新しい名前"
        user.set_password("another-password")
        user.save()
        client_class.return_value.send.reset_mock()

        notify.notify_update_db(model_name="User", instance=user)

        text = sent_texts(client_class)
        self.assertIn("display_name", text)
        self.assertNotIn("password", text)
        self.assertNotIn(user.password, text)

    def test_nothing_is_sent_without_webhook(self, client_class):
        with override_settings(SLACK_WEBHOOK_URL_LOG=""):
            self._create_user()

        client_class.assert_not_called()
