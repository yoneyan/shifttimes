"""運営向け Slack 通知（shifttimes.notify）と管理画面 URL 生成のテスト。"""

from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from notice.models import Notice
from shifttimes import notify
from shifttimes.utils import get_admin_history_url, get_admin_url

WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/XXXX"


@override_settings(ADMIN_DOMAIN_URL="https://admin.example.com")
class AdminUrlTests(TestCase):
    def test_admin_urls_point_to_change_and_history_pages(self):
        notice = Notice.objects.create(type1=Notice.SERVICE, title="メンテナンス")

        self.assertEqual(
            get_admin_url(notice), f"https://admin.example.com/admin/notice/notice/{notice.pk}/change/"
        )
        self.assertEqual(
            get_admin_history_url(notice), f"https://admin.example.com/admin/notice/notice/{notice.pk}/history/"
        )


class NotifyTests(TestCase):
    def setUp(self):
        # 通知はモデルの保存シグナルからも呼ばれるため、テスト対象の呼び出し以外では Webhook を設定しない
        self.notice = Notice.objects.create(type1=Notice.SERVICE, title="メンテナンス")
        patcher = mock.patch.object(notify, "WebhookClient")
        self.webhook_client = patcher.start()
        self.addCleanup(patcher.stop)

    def _sent_attachment(self):
        self.webhook_client.assert_called_once_with(WEBHOOK_URL)
        send = self.webhook_client.return_value.send
        send.assert_called_once()
        return send.call_args.kwargs["attachments"][0]

    def test_nothing_is_sent_without_webhook_url(self):
        notify.notify_insert_db("Notice", self.notice)
        notify.notify_update_db("Notice", self.notice)
        notify.notify_delete_db("Notice", self.notice)
        notify.notice_payment("group", "customer.subscription.created", {})

        self.webhook_client.assert_not_called()

    @override_settings(SLACK_WEBHOOK_URL_LOG=WEBHOOK_URL)
    def test_notify_insert_lists_fields(self):
        notify.notify_insert_db("Notice", self.notice)

        attachment = self._sent_attachment()
        self.assertEqual(attachment["color"], "good")
        self.assertEqual(attachment["title"], "[Notice]")
        self.assertIn("title: メンテナンス", attachment["text"])
        self.assertIn(f"/admin/notice/notice/{self.notice.pk}/change/", attachment["text"])

    @override_settings(SLACK_WEBHOOK_URL_LOG=WEBHOOK_URL)
    def test_notify_delete(self):
        notify.notify_delete_db("Notice", self.notice)

        attachment = self._sent_attachment()
        self.assertEqual(attachment["color"], "danger")
        self.assertIn("レコードが削除されました", attachment["text"])
        self.assertIn(f"/admin/notice/notice/{self.notice.pk}/history/", attachment["text"])

    @override_settings(SLACK_WEBHOOK_URL_LOG=WEBHOOK_URL)
    def test_notify_update_is_skipped_without_previous_history(self):
        notify.notify_update_db("Notice", self.notice)

        self.webhook_client.return_value.send.assert_not_called()

    def test_notify_update_reports_changed_fields(self):
        self.notice.title = "障害のお知らせ"
        self.notice.save()

        with override_settings(SLACK_WEBHOOK_URL_LOG=WEBHOOK_URL):
            notify.notify_update_db("Notice", self.notice)

        attachment = self._sent_attachment()
        self.assertIn("title: メンテナンス -> 障害のお知らせ", attachment["text"])
        self.assertNotIn("type1:", attachment["text"])

    @override_settings(SLACK_WEBHOOK_URL_LOG=WEBHOOK_URL)
    def test_notice_payment(self):
        data = {
            "id": "sub_123",
            "name": "shop",
            "plan_amount": 1980,
            "plan_interval": "month",
            "start": timezone.datetime(2026, 1, 1),
            "end": timezone.datetime(2026, 2, 1),
            "status": "active",
        }

        notify.notice_payment("group", "customer.subscription.deleted", data)

        send = self.webhook_client.return_value.send
        self.assertEqual(
            send.call_args.kwargs["text"], "[group(customer.subscription.deleted)] sub_123-shop [1980円(/month)]"
        )
        self.assertEqual(send.call_args.kwargs["attachments"][0]["color"], "danger")

    def test_get_color(self):
        self.assertEqual(notify.get_color("customer.subscription.created"), "warning")
        self.assertEqual(notify.get_color("customer.subscription.updated"), "good")
        self.assertEqual(notify.get_color("customer.subscription.deleted"), "danger")
        self.assertIsNone(notify.get_color("invoice.paid"))
