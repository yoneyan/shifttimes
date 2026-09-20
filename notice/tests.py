from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notice.models import Notice


class NoticeManageTests(TestCase):
    """運営向けの通知管理ページ"""

    def setUp(self):
        user_model = get_user_model()
        self.staff_user = user_model.objects.create(
            username="staff", username_jp="運営", email="staff@example.com",
            is_active=True, is_staff=True,
        )
        self.member = user_model.objects.create(
            username="member", username_jp="メンバー", email="member@example.com", is_active=True,
        )
        self.manage_url = reverse("notice:manage")
        self.now = timezone.now().replace(second=0, microsecond=0)

    def _post_notice(self, **overrides):
        data = {
            "type1": Notice.SERVICE,
            "title": "メンテナンスのお知らせ",
            "body": "3/1 の 0:00 〜 2:00 にメンテナンスを行います。",
            "start_at": self.now.strftime("%Y-%m-%dT%H:%M"),
            "end_at": (self.now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M"),
            "is_active": "on",
        }
        data.update(overrides)
        return self.client.post(self.manage_url, data)

    def test_page_requires_staff(self):
        self.client.force_login(self.member)
        member_response = self.client.get(self.manage_url)

        self.client.force_login(self.staff_user)
        staff_response = self.client.get(self.manage_url)

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(staff_response.status_code, 200)

    def test_staff_can_add_notice(self):
        self.client.force_login(self.staff_user)

        response = self._post_notice(is_important="on")

        self.assertRedirects(response, self.manage_url)
        notice = Notice.objects.get(title="メンテナンスのお知らせ")
        self.assertEqual(notice.type1, Notice.SERVICE)
        self.assertTrue(notice.is_active)
        self.assertTrue(notice.is_important)
        self.assertEqual(notice.end_at, self.now + timedelta(days=7))

    def test_added_notice_is_shown_on_notice_page(self):
        self.client.force_login(self.staff_user)
        self._post_notice()

        self.client.force_login(self.member)
        response = self.client.get(reverse("notice:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "メンテナンスのお知らせ")

    def test_notice_without_end_at_is_kept_open_ended(self):
        self.client.force_login(self.staff_user)

        self._post_notice(end_at="")

        notice = Notice.objects.get(title="メンテナンスのお知らせ")
        self.assertIsNone(notice.end_at)

    def test_staff_can_edit_notice(self):
        self.client.force_login(self.staff_user)
        self._post_notice()
        notice = Notice.objects.get(title="メンテナンスのお知らせ")

        self._post_notice(
            notice_id=str(notice.id), title="メンテナンス完了のお知らせ", is_active="",
        )

        notice.refresh_from_db()
        self.assertEqual(notice.title, "メンテナンス完了のお知らせ")
        self.assertFalse(notice.is_active)
        self.assertEqual(Notice.objects.count(), 1)

    def test_end_at_must_be_after_start_at(self):
        self.client.force_login(self.staff_user)

        self._post_notice(end_at=(self.now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))

        self.assertFalse(Notice.objects.exists())

    def test_staff_can_delete_notice(self):
        self.client.force_login(self.staff_user)
        self._post_notice()
        notice = Notice.objects.get(title="メンテナンスのお知らせ")

        response = self.client.post(reverse("notice:notice_delete", args=[notice.id]))

        self.assertRedirects(response, self.manage_url)
        self.assertFalse(Notice.objects.filter(id=notice.id).exists())

    def test_member_cannot_delete_notice(self):
        self.client.force_login(self.staff_user)
        self._post_notice()
        notice = Notice.objects.get(title="メンテナンスのお知らせ")

        self.client.force_login(self.member)
        response = self.client.post(reverse("notice:notice_delete", args=[notice.id]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Notice.objects.filter(id=notice.id).exists())

    def test_list_shows_status_of_each_notice(self):
        Notice.objects.create(
            type1=Notice.ETC, title="掲示前", start_at=self.now + timedelta(days=1),
        )
        Notice.objects.create(
            type1=Notice.ETC, title="終了", start_at=self.now - timedelta(days=2),
            end_at=self.now - timedelta(days=1),
        )
        Notice.objects.create(
            type1=Notice.ETC, title="停止", start_at=self.now, is_active=False,
        )
        Notice.objects.create(type1=Notice.ETC, title="掲示中", start_at=self.now)
        self.client.force_login(self.staff_user)

        rows = self.client.get(self.manage_url).context["notice_rows"]

        statuses = {row["notice"].title: row["status"]["label"] for row in rows}
        self.assertEqual(statuses["掲示前"], "掲示前")
        self.assertEqual(statuses["終了"], "終了")
        self.assertEqual(statuses["停止"], "停止")
        self.assertEqual(statuses["掲示中"], "掲示中")
