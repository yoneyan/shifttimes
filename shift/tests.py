from datetime import date, time, timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from custom_auth.models import Group, UserGroup
from shift import slack
from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
    SlackNotificationSetting,
    TimeSlot,
)


class ShiftViewTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="user",
            username_jp="ユーザー",
            email="user@example.com",
            is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin",
            username_jp="管理者",
            email="admin@example.com",
            is_active=True,
        )
        self.other_user = user_model.objects.create(
            username="other",
            username_jp="別ユーザー",
            email="other@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.user, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.time_slot = TimeSlot.objects.create(
            group=self.group,
            name="早番",
            start_time=time(9, 0),
            end_time=time(13, 0),
        )
        self.deadline = ShiftDeadline.objects.create(
            group=self.group,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 30),
            deadline_date=date(2100, 1, 1),
        )

    def test_entry_table_requires_group_membership(self):
        self.client.force_login(self.user)

        allowed = self.client.get(reverse("shift:entry_table", args=[self.group.id]))
        forbidden = self.client.get(reverse("shift:entry_table", args=[self.other_group.id]))

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(forbidden.status_code, 403)

    def test_index_lists_accessible_group(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "店舗")
        self.assertNotContains(response, "別店舗")

    def test_index_shows_draft_count_and_calendar_link(self):
        # 未確定件数は未来分だけを数えるため、必ず今日以降の日付を使う
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=timezone.now().date() + timedelta(days=1),
            time_slot=self.time_slot,
            status=ShiftEntry.AVAILABLE,
            is_draft=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "未確定 1 件")
        self.assertContains(response, reverse("shift:shift_calendar", args=[self.group.id]))
        self.assertContains(response, reverse("shift:entry_table", args=[self.group.id]))

    def test_shift_calendar_renders_month_grid_with_entries(self):
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 10),
            time_slot=self.time_slot,
            status=ShiftEntry.MAYBE,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:shift_calendar", args=[self.group.id]), {"month": "2026-06"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2026年6月")
        self.assertContains(response, "cal-chip")
        # 日付セルからその日の登録画面へ遷移できる
        self.assertContains(response, "%s?date=2026-06-10" % reverse("shift:entry_table", args=[self.group.id]))

    def test_shift_calendar_month_navigation(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:shift_calendar", args=[self.group.id]), {"month": "2026-06"})

        self.assertContains(response, "?month=2026-05")
        self.assertContains(response, "?month=2026-07")

    def test_schedule_calendar_requires_admin_but_member_view_does_not(self):
        closed_type = OpeningScheduleType.objects.create(
            group=self.group, name="休校", blocks_shift_input=True,
        )
        DateOpeningSchedule.objects.create(
            group=self.group, work_date=date(2026, 6, 7), schedule_type=closed_type,
        )
        self.client.force_login(self.user)
        member_admin_page = self.client.get(reverse("shift:schedule", args=[self.group.id]))
        member_view = self.client.get(reverse("shift:schedule_member", args=[self.group.id]), {"month": "2026-06"})

        self.assertEqual(member_admin_page.status_code, 403)
        self.assertEqual(member_view.status_code, 200)
        self.assertContains(member_view, "休校")

    def test_entry_table_post_saves_shift_request_for_one_date(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("shift:entry_table", args=[self.group.id]),
            {
                "month": "2026-06",
                "work_date": "2026-06-10",
                "status_%d" % self.time_slot.id: ShiftEntry.AVAILABLE,
                "comment": "午前だけ可能",
            },
        )

        self.assertRedirects(
            response,
            "%s?month=2026-06" % reverse("shift:entry_table", args=[self.group.id]),
        )
        entry = ShiftEntry.objects.get(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 10),
            time_slot=self.time_slot,
        )
        self.assertEqual(entry.status, ShiftEntry.AVAILABLE)
        self.assertEqual(entry.note, "午前だけ可能")
        self.assertTrue(entry.is_draft)

    def test_entry_table_post_clears_entry_when_status_is_empty(self):
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 10),
            time_slot=self.time_slot,
            status=ShiftEntry.AVAILABLE,
        )
        self.client.force_login(self.user)

        self.client.post(
            reverse("shift:entry_table", args=[self.group.id]),
            {
                "month": "2026-06",
                "work_date": "2026-06-10",
                "status_%d" % self.time_slot.id: "",
                "comment": "",
            },
        )

        self.assertFalse(
            ShiftEntry.objects.filter(group=self.group, user=self.user, work_date=date(2026, 6, 10)).exists()
        )

    def test_entry_table_weekday_bulk_saves_every_matching_date(self):
        self.client.force_login(self.user)

        # 2026年6月の月曜日は 1, 8, 15, 22, 29
        self.client.post(
            reverse("shift:entry_table", args=[self.group.id]),
            {
                "month": "2026-06",
                "weekday": "0",
                "status_%d" % self.time_slot.id: ShiftEntry.UNAVAILABLE,
                "comment": "",
            },
        )

        self.assertEqual(
            list(
                ShiftEntry.objects.filter(group=self.group, user=self.user)
                .order_by("work_date")
                .values_list("work_date", flat=True)
            ),
            [date(2026, 6, 1), date(2026, 6, 8), date(2026, 6, 15), date(2026, 6, 22), date(2026, 6, 29)],
        )

    def test_entry_table_post_skips_closed_date(self):
        closed_type = OpeningScheduleType.objects.create(
            group=self.group,
            name="休校",
            blocks_shift_input=True,
        )
        DateOpeningSchedule.objects.create(
            group=self.group,
            work_date=date(2026, 6, 10),
            schedule_type=closed_type,
        )
        self.client.force_login(self.user)

        self.client.post(
            reverse("shift:entry_table", args=[self.group.id]),
            {
                "month": "2026-06",
                "work_date": "2026-06-10",
                "status_%d" % self.time_slot.id: ShiftEntry.AVAILABLE,
                "comment": "",
            },
        )

        self.assertFalse(ShiftEntry.objects.filter(group=self.group, user=self.user).exists())

    def test_entry_table_shows_existing_entries(self):
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 10),
            time_slot=self.time_slot,
            status=ShiftEntry.MAYBE,
            note="相談",
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:entry_table", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-date="2026-06-10"')
        self.assertContains(response, "相談")

    def test_entry_table_post_ignores_date_outside_open_period(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("shift:entry_table", args=[self.group.id]),
            {
                "month": "2026-06",
                "work_date": "2026-07-01",
                "status_%d" % self.time_slot.id: ShiftEntry.AVAILABLE,
                "comment": "",
            },
        )

        self.assertRedirects(
            response,
            "%s?month=2026-06" % reverse("shift:entry_table", args=[self.group.id]),
        )
        self.assertFalse(ShiftEntry.objects.filter(group=self.group, user=self.user).exists())

    def test_entry_table_falls_back_to_default_period_without_deadline(self):
        self.deadline.delete()
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:entry_table", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "提出期限は未設定です")

    def test_schedule_settings_deadline_form_creates_and_updates(self):
        self.client.force_login(self.admin_user)

        create_response = self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "deadline",
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "deadline_date": "2026-07-20",
                "note": "8月分",
            },
        )
        self.assertRedirects(create_response, reverse("shift:schedule_settings", args=[self.group.id]))
        created = ShiftDeadline.objects.get(group=self.group, period_start=date(2026, 8, 1))
        self.assertEqual(created.deadline_date, date(2026, 7, 20))
        self.assertEqual(created.note, "8月分")

        update_response = self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "deadline",
                "deadline_id": str(created.id),
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "deadline_date": "2026-07-25",
                "note": "8月分（延長）",
            },
        )
        self.assertRedirects(update_response, reverse("shift:schedule_settings", args=[self.group.id]))
        created.refresh_from_db()
        self.assertEqual(created.deadline_date, date(2026, 7, 25))
        self.assertEqual(created.note, "8月分（延長）")
        self.assertEqual(ShiftDeadline.objects.filter(group=self.group).count(), 2)

    def test_schedule_settings_schedule_type_creates_and_updates(self):
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "schedule_type",
                "name": "短縮授業",
                "display_order": "40",
                "is_active": "on",
            },
        )
        created = OpeningScheduleType.objects.get(group=self.group, name="短縮授業")
        self.assertFalse(created.blocks_shift_input)
        self.assertTrue(created.is_active)

        self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "schedule_type",
                "schedule_type_id": str(created.id),
                "name": "短縮授業",
                "display_order": "40",
                "blocks_shift_input": "on",
            },
        )
        created.refresh_from_db()
        self.assertTrue(created.blocks_shift_input)
        self.assertFalse(created.is_active)

    def test_schedule_type_color_is_saved_and_used_on_calendar(self):
        """開講区分ごとに表示色を設定でき、カレンダーの凡例にも反映される"""
        self.client.force_login(self.admin_user)
        settings_url = reverse("shift:schedule_settings", args=[self.group.id])

        self.client.post(settings_url, {
            "action": "schedule_type",
            "name": "研修日",
            "color": "#AB12CD",
            "display_order": "40",
            "is_active": "on",
        })
        created = OpeningScheduleType.objects.get(group=self.group, name="研修日")
        self.assertEqual(created.color, "#ab12cd")

        self.client.post(settings_url, {
            "action": "schedule_type",
            "schedule_type_id": str(created.id),
            "name": "研修日",
            "color": "#123456",
            "display_order": "40",
            "is_active": "on",
        })
        created.refresh_from_db()
        self.assertEqual(created.color, "#123456")

        legend = self.client.get(reverse("shift:schedule", args=[self.group.id])).context["legend"]
        self.assertIn({"name": "研修日", "color": "#123456"}, legend)

    def test_schedule_type_rejects_invalid_color(self):
        schedule_type = OpeningScheduleType.objects.create(
            group=self.group, name="研修日", color="#123456",
        )
        self.client.force_login(self.admin_user)

        self.client.post(reverse("shift:schedule_settings", args=[self.group.id]), {
            "action": "schedule_type",
            "schedule_type_id": str(schedule_type.id),
            "name": "研修日",
            "color": "red; background:url(x)",
            "display_order": "100",
            "is_active": "on",
        })

        schedule_type.refresh_from_db()
        self.assertEqual(schedule_type.color, "#123456")

    def test_schedule_settings_suggests_unused_color(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("shift:schedule_settings", args=[self.group.id]))

        used_colors = set(
            OpeningScheduleType.objects.filter(group=self.group).values_list("color", flat=True)
        )
        self.assertNotIn(response.context["default_schedule_type_color"], used_colors)

    def test_schedule_page_creates_default_schedule_types_for_admin(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("shift:schedule", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "開講")
        self.assertContains(response, "休校")
        self.assertContains(response, "自習室")

    def test_schedule_settings_saves_single_date(self):
        closed_type = OpeningScheduleType.objects.create(
            group=self.group,
            name="臨時休校",
            blocks_shift_input=True,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "schedule_date",
                "month": "2026-06",
                "work_date": "2026-06-08",
                "schedule_type": str(closed_type.id),
                "note": "創立記念日",
            },
        )

        self.assertRedirects(
            response,
            "%s?month=2026-06" % reverse("shift:schedule_settings", args=[self.group.id]),
        )
        schedule = DateOpeningSchedule.objects.get(group=self.group, work_date=date(2026, 6, 8))
        self.assertEqual(schedule.schedule_type, closed_type)
        self.assertEqual(schedule.note, "創立記念日")

    def test_schedule_settings_clears_date_when_type_is_empty(self):
        closed_type = OpeningScheduleType.objects.create(
            group=self.group,
            name="臨時休校",
            blocks_shift_input=True,
        )
        DateOpeningSchedule.objects.create(
            group=self.group,
            work_date=date(2026, 6, 8),
            schedule_type=closed_type,
        )
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "schedule_date",
                "month": "2026-06",
                "work_date": "2026-06-08",
                "schedule_type": "",
                "note": "",
            },
        )

        self.assertFalse(
            DateOpeningSchedule.objects.filter(group=self.group, work_date=date(2026, 6, 8)).exists()
        )

    def test_schedule_settings_weekday_bulk_sets_every_matching_date(self):
        closed_type = OpeningScheduleType.objects.create(
            group=self.group,
            name="休校",
            blocks_shift_input=True,
        )
        self.client.force_login(self.admin_user)

        # 2026年6月の日曜日は 7, 14, 21, 28
        self.client.post(
            reverse("shift:schedule_settings", args=[self.group.id]),
            {
                "action": "schedule_weekday",
                "month": "2026-06",
                "weekday": "6",
                "schedule_type": str(closed_type.id),
                "note": "定休日",
            },
        )

        self.assertEqual(
            list(
                DateOpeningSchedule.objects.filter(group=self.group)
                .order_by("work_date")
                .values_list("work_date", flat=True)
            ),
            [date(2026, 6, 7), date(2026, 6, 14), date(2026, 6, 21), date(2026, 6, 28)],
        )

    def test_schedule_settings_requires_group_admin(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:schedule_settings", args=[self.group.id]))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:schedule_settings", args=[self.group.id]))

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 200)

    def test_schedule_index_requires_group_admin(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:schedule_index"))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:schedule_index"))

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 302)

    def test_admin_nav_links_are_admin_only(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:index"))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:index"))

        self.assertNotContains(member_response, 'href="/shift/schedule/"')
        self.assertNotContains(member_response, 'href="/shift/summary/"')
        self.assertContains(admin_response, 'href="/shift/schedule/"')
        self.assertContains(admin_response, 'href="/shift/summary/"')

    def test_summary_requires_group_admin(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:summary", args=[self.group.id]))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:summary", args=[self.group.id]))

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 200)

    def test_summary_excludes_drafts_from_submission_status(self):
        """下書きのまま提出されていない希望は「提出済み」に数えない"""
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 1),
            time_slot=self.time_slot,
            status=ShiftEntry.AVAILABLE,
            is_draft=True,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]),
            {"from": "2026-06-01", "days": "1"},
        )

        self.assertEqual(response.status_code, 200)
        row = self._summary_row(response, self.user)
        self.assertEqual(row["answered_days"], 0)
        self.assertEqual(row["available_days"], 0)
        self.assertEqual(row["status"], "none")
        self.assertEqual(response.context["stats"]["none"], 2)

    def test_summary_counts_submitted_entries_and_daily_totals(self):
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 1),
            time_slot=self.time_slot,
            status=ShiftEntry.AVAILABLE,
        )
        ShiftEntry.objects.create(
            group=self.group,
            user=self.admin_user,
            work_date=date(2026, 6, 1),
            time_slot=self.time_slot,
            status=ShiftEntry.MAYBE,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]),
            {"from": "2026-06-01", "days": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["done"], 2)
        self.assertEqual(response.context["stats"]["target_days"], 1)
        self.assertEqual(self._summary_row(response, self.user)["available_days"], 1)
        # 要相談は「勤務可能日」には数えない
        self.assertEqual(self._summary_row(response, self.admin_user)["available_days"], 0)

        slot_counts = response.context["day_totals"][0]["slot_counts"][0]
        self.assertEqual(slot_counts["available"], 1)
        self.assertEqual(slot_counts["maybe"], 1)

    def test_summary_excludes_blocked_days_from_target_days(self):
        blocked_type = OpeningScheduleType.objects.create(
            group=self.group, name="休校", blocks_shift_input=True,
        )
        DateOpeningSchedule.objects.create(
            group=self.group, work_date=date(2026, 6, 2), schedule_type=blocked_type,
        )
        ShiftEntry.objects.create(
            group=self.group,
            user=self.user,
            work_date=date(2026, 6, 1),
            time_slot=self.time_slot,
            status=ShiftEntry.AVAILABLE,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]),
            {"from": "2026-06-01", "days": "2"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["target_days"], 1)
        self.assertEqual(response.context["stats"]["blocked_days"], 1)
        self.assertEqual(self._summary_row(response, self.user)["status"], "done")

    def test_summary_defaults_to_whole_month_of_start_date(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]), {"from": "2026-06-01"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["days"], 30)
        self.assertEqual(response.context["date_to"], date(2026, 6, 30))
        # 暦月ぴったりの期間なら前後の移動も暦月単位
        self.assertTrue(response.context["is_whole_month"])
        self.assertEqual(response.context["next_from"], date(2026, 7, 1))
        self.assertEqual(response.context["next_days"], 31)

    def test_summary_shows_matching_deadline(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]),
            {"from": "2026-06-01", "days": "7"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["deadline"], self.deadline)
        self.assertFalse(response.context["deadline_passed"])

    @staticmethod
    def _summary_row(response, user):
        for row in response.context["member_rows"]:
            if row["member"].id == user.id:
                return row
        raise AssertionError("member_rows に %s がいません" % user.username)


class TimeSlotSettingsTests(TestCase):
    """シフト・スケジュール設定の勤務時間（時間帯）設定"""

    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create(
            username="member", username_jp="メンバー", email="member@example.com", is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="slot-admin", username_jp="管理者", email="slot-admin@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.member, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.settings_url = reverse("shift:schedule_settings", args=[self.group.id])

    def _post_time_slot(self, **overrides):
        data = {
            "action": "time_slot",
            "name": "早番",
            "start_time": "09:00",
            "end_time": "13:00",
            "is_active": "on",
        }
        data.update(overrides)
        return self.client.post(self.settings_url, data)

    def test_settings_page_creates_default_time_slots(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(self.settings_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TimeSlot.objects.filter(group=self.group).count(), 3)
        self.assertEqual(
            [row["time_slot"].name for row in response.context["time_slot_rows"]],
            ["早番", "中番", "遅番"],
        )

    def test_admin_can_add_time_slot(self):
        self.client.force_login(self.admin_user)
        TimeSlot.objects.filter(group=self.group).delete()

        response = self._post_time_slot(name="夜番", start_time="18:00", end_time="22:00")

        self.assertRedirects(response, self.settings_url)
        time_slot = TimeSlot.objects.get(group=self.group, name="夜番")
        self.assertEqual(time_slot.start_time, time(18, 0))
        self.assertEqual(time_slot.end_time, time(22, 0))
        self.assertTrue(time_slot.is_active)

    def test_admin_can_edit_and_disable_time_slot(self):
        time_slot = TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        self.client.force_login(self.admin_user)

        response = self._post_time_slot(
            time_slot_id=time_slot.id, name="午前", start_time="10:00", end_time="14:00", is_active="",
        )

        self.assertRedirects(response, self.settings_url)
        time_slot.refresh_from_db()
        self.assertEqual(time_slot.name, "午前")
        self.assertEqual(time_slot.start_time, time(10, 0))
        self.assertFalse(time_slot.is_active)

    def test_duplicate_name_in_same_group_is_rejected(self):
        TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        self.client.force_login(self.admin_user)

        self._post_time_slot(name="早番", start_time="07:00", end_time="11:00")

        self.assertEqual(TimeSlot.objects.filter(group=self.group, name="早番").count(), 1)
        self.assertEqual(TimeSlot.objects.get(group=self.group, name="早番").start_time, time(9, 0))

    def test_end_time_must_be_after_start_time(self):
        self.client.force_login(self.admin_user)
        TimeSlot.objects.filter(group=self.group).delete()

        self._post_time_slot(name="深夜", start_time="22:00", end_time="06:00")

        self.assertFalse(TimeSlot.objects.filter(group=self.group, name="深夜").exists())

    def test_member_cannot_change_time_slots(self):
        self.client.force_login(self.member)

        response = self._post_time_slot(name="勝手に追加")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(TimeSlot.objects.filter(name="勝手に追加").exists())

    def test_unused_time_slot_can_be_deleted(self):
        time_slot = TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("shift:time_slot_delete", args=[self.group.id, time_slot.id])
        )

        self.assertRedirects(response, self.settings_url)
        self.assertFalse(TimeSlot.objects.filter(id=time_slot.id).exists())

    def test_used_time_slot_is_kept_and_can_only_be_disabled(self):
        time_slot = TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        ShiftEntry.objects.create(
            group=self.group,
            user=self.member,
            work_date=date(2026, 6, 1),
            time_slot=time_slot,
            status=ShiftEntry.AVAILABLE,
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("shift:time_slot_delete", args=[self.group.id, time_slot.id])
        )

        self.assertRedirects(response, self.settings_url)
        self.assertTrue(TimeSlot.objects.filter(id=time_slot.id).exists())

        rows = self.client.get(self.settings_url).context["time_slot_rows"]
        used_row = next(row for row in rows if row["time_slot"].id == time_slot.id)
        self.assertEqual(used_row["entry_count"], 1)
        self.assertFalse(used_row["can_delete"])

    def test_time_slot_of_other_group_is_not_editable(self):
        other_slot = TimeSlot.objects.create(
            group=self.other_group, name="他店の早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("shift:time_slot_delete", args=[self.group.id, other_slot.id])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(TimeSlot.objects.filter(id=other_slot.id).exists())

    def test_summary_keeps_disabled_time_slot_with_submitted_entries(self):
        """無効にした勤務時間でも、提出済みの希望が残っていれば集計に表示する"""
        time_slot = TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        ShiftEntry.objects.create(
            group=self.group,
            user=self.member,
            work_date=date(2026, 6, 1),
            time_slot=time_slot,
            status=ShiftEntry.AVAILABLE,
        )
        time_slot.is_active = False
        time_slot.save(update_fields=["is_active"])
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse("shift:summary", args=[self.group.id]),
            {"from": "2026-06-01", "days": "1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("早番", [slot.name for slot in response.context["time_slots"]])

    def test_entry_table_shows_only_own_group_time_slots(self):
        TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        TimeSlot.objects.create(
            group=self.other_group, name="他店の早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        TimeSlot.objects.create(
            group=self.group, name="無効な時間帯", start_time=time(20, 0), end_time=time(22, 0),
            is_active=False,
        )
        self.client.force_login(self.member)

        response = self.client.get(reverse("shift:entry_table", args=[self.group.id]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([slot.name for slot in response.context["time_slots"]], ["早番"])


class AttendanceTests(TestCase):
    """勤怠管理（手動入力・打刻・管理者による有効化）"""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="user",
            username_jp="ユーザー",
            email="user@example.com",
            is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin",
            username_jp="管理者",
            email="admin@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.user, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.setting = AttendanceSetting.objects.create(group=self.group, is_enabled=True)
        self.today = timezone.now().date()
        self.month_value = self.today.strftime("%Y-%m")

    def _attendance_url(self):
        return reverse("shift:attendance", args=[self.group.id])

    # ----- 機能の有効・無効 -----

    def test_attendance_page_is_unavailable_while_feature_is_disabled(self):
        self.setting.is_enabled = False
        self.setting.save()
        self.client.force_login(self.user)

        response = self.client.get(self._attendance_url())

        self.assertRedirects(response, reverse("shift:index"))

    def test_attendance_page_requires_group_membership(self):
        AttendanceSetting.objects.create(group=self.other_group, is_enabled=True)
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:attendance", args=[self.other_group.id]))

        self.assertEqual(response.status_code, 403)

    def test_attendance_nav_link_appears_only_while_enabled(self):
        self.client.force_login(self.user)
        enabled_response = self.client.get(reverse("shift:index"))

        self.setting.is_enabled = False
        self.setting.save()
        disabled_response = self.client.get(reverse("shift:index"))

        self.assertContains(enabled_response, reverse("shift:attendance_index"))
        self.assertNotContains(disabled_response, reverse("shift:attendance_index"))

    def test_settings_page_requires_group_admin(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:attendance_settings", args=[self.group.id]))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:attendance_settings", args=[self.group.id]))

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 200)

    def test_admin_can_enable_and_disable_the_feature(self):
        self.setting.is_enabled = False
        self.setting.save()
        self.client.force_login(self.admin_user)

        enable_response = self.client.post(
            reverse("shift:attendance_settings", args=[self.group.id]),
            {"is_enabled": "on", "allow_manual_input": "on", "allow_clock_button": "on"},
        )
        self.assertRedirects(enable_response, reverse("shift:attendance_settings", args=[self.group.id]))
        self.setting.refresh_from_db()
        self.assertTrue(self.setting.is_enabled)

        self.client.post(
            reverse("shift:attendance_settings", args=[self.group.id]),
            {"allow_manual_input": "on", "allow_clock_button": "on"},
        )
        self.setting.refresh_from_db()
        self.assertFalse(self.setting.is_enabled)

    def test_enabling_without_any_input_method_is_rejected(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("shift:attendance_settings", args=[self.group.id]),
            {"is_enabled": "on"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "少なくとも一方を許可してください")
        self.setting.refresh_from_db()
        self.assertTrue(self.setting.allow_manual_input)

    def test_attendance_index_redirects_when_only_one_group_is_enabled(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:attendance_index"))

        self.assertRedirects(response, self._attendance_url())

    def test_attendance_index_lists_only_enabled_groups(self):
        second_group = Group.objects.create(name="second-shop", name_jp="2号店")
        UserGroup.objects.create(user=self.user, group=second_group)
        AttendanceSetting.objects.create(group=second_group, is_enabled=True)
        disabled_group = Group.objects.create(name="third-shop", name_jp="3号店")
        UserGroup.objects.create(user=self.user, group=disabled_group)
        self.client.force_login(self.user)

        response = self.client.get(reverse("shift:attendance_index"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "2号店")
        self.assertNotContains(response, "3号店")

    # ----- 打刻（出勤ボタン・退勤ボタン） -----

    def test_clock_in_and_clock_out_record_current_time(self):
        self.client.force_login(self.user)

        self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "in"})
        record = AttendanceRecord.objects.get(group=self.group, user=self.user, work_date=self.today)
        self.assertIsNotNone(record.start_time)
        self.assertIsNone(record.end_time)
        self.assertEqual(record.source, AttendanceRecord.CLOCK)

        self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "out"})
        record.refresh_from_db()
        self.assertIsNotNone(record.end_time)

    def test_clock_out_closes_previous_day_record_for_overnight_work(self):
        yesterday = self.today - timedelta(days=1)
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=yesterday,
            start_time=time(22, 0), source=AttendanceRecord.CLOCK,
        )
        self.client.force_login(self.user)

        self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "out"})

        record = AttendanceRecord.objects.get(group=self.group, user=self.user, work_date=yesterday)
        self.assertIsNotNone(record.end_time)
        self.assertFalse(AttendanceRecord.objects.filter(work_date=self.today).exists())

    def test_clock_out_without_clock_in_is_rejected(self):
        self.client.force_login(self.user)

        self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "out"})

        self.assertFalse(AttendanceRecord.objects.filter(group=self.group, user=self.user).exists())

    def test_clock_button_can_be_disabled_by_admin(self):
        self.setting.allow_clock_button = False
        self.setting.save()
        self.client.force_login(self.user)

        page = self.client.get(self._attendance_url())
        response = self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "in"})

        self.assertNotContains(page, reverse("shift:attendance_clock", args=[self.group.id]))
        self.assertRedirects(response, self._attendance_url())
        self.assertFalse(AttendanceRecord.objects.filter(group=self.group, user=self.user).exists())

    # ----- 手動入力（一覧表） -----

    def test_manual_input_saves_record(self):
        self.client.force_login(self.user)

        response = self.client.post(
            self._attendance_url(),
            {
                "month": self.month_value,
                "work_date": self.today.isoformat(),
                "start_time": "09:00",
                "end_time": "18:30",
                "break_minutes": "60",
                "note": "通常勤務",
            },
        )

        self.assertRedirects(response, "%s?month=%s" % (self._attendance_url(), self.month_value))
        record = AttendanceRecord.objects.get(group=self.group, user=self.user, work_date=self.today)
        self.assertEqual(record.start_time, time(9, 0))
        self.assertEqual(record.end_time, time(18, 30))
        self.assertEqual(record.break_minutes, 60)
        self.assertEqual(record.note, "通常勤務")
        self.assertEqual(record.source, AttendanceRecord.MANUAL)
        self.assertEqual(record.worked_time_display, "8:30")

    def test_manual_input_deletes_record_when_times_are_cleared(self):
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(18, 0),
        )
        self.client.force_login(self.user)

        self.client.post(
            self._attendance_url(),
            {
                "month": self.month_value,
                "work_date": self.today.isoformat(),
                "start_time": "",
                "end_time": "",
                "break_minutes": "",
                "note": "",
            },
        )

        self.assertFalse(AttendanceRecord.objects.filter(group=self.group, user=self.user).exists())

    def test_manual_input_rejects_end_time_without_start_time(self):
        self.client.force_login(self.user)

        self.client.post(
            self._attendance_url(),
            {
                "month": self.month_value,
                "work_date": self.today.isoformat(),
                "start_time": "",
                "end_time": "18:00",
                "break_minutes": "",
                "note": "",
            },
        )

        self.assertFalse(AttendanceRecord.objects.filter(group=self.group, user=self.user).exists())

    def test_manual_input_can_be_disabled_by_admin(self):
        self.setting.allow_manual_input = False
        self.setting.save()
        self.client.force_login(self.user)

        page = self.client.get(self._attendance_url())
        self.client.post(
            self._attendance_url(),
            {
                "month": self.month_value,
                "work_date": self.today.isoformat(),
                "start_time": "09:00",
                "end_time": "18:00",
                "break_minutes": "0",
                "note": "",
            },
        )

        self.assertContains(page, "手動入力が許可されていません")
        self.assertFalse(AttendanceRecord.objects.filter(group=self.group, user=self.user).exists())

    def test_attendance_page_shows_month_totals(self):
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(18, 0), break_minutes=60,
        )
        self.client.force_login(self.user)

        response = self.client.get(self._attendance_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "勤務日数 1 日")
        self.assertContains(response, "実働合計 8:00")

    # ----- 管理者の集計・修正 -----

    def test_summary_requires_group_admin(self):
        self.client.force_login(self.user)
        member_response = self.client.get(reverse("shift:attendance_summary", args=[self.group.id]))

        self.client.force_login(self.admin_user)
        admin_response = self.client.get(reverse("shift:attendance_summary", args=[self.group.id]))

        self.assertEqual(member_response.status_code, 403)
        self.assertEqual(admin_response.status_code, 200)

    def test_admin_can_fix_member_record_even_while_manual_input_is_disabled(self):
        self.setting.allow_manual_input = False
        self.setting.save()
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("shift:attendance_summary", args=[self.group.id]),
            {
                "month": self.month_value,
                "user_id": str(self.user.id),
                "work_date": self.today.isoformat(),
                "start_time": "10:00",
                "end_time": "19:00",
                "break_minutes": "30",
                "note": "打刻忘れのため修正",
            },
        )

        record = AttendanceRecord.objects.get(group=self.group, user=self.user, work_date=self.today)
        self.assertEqual(record.start_time, time(10, 0))
        self.assertEqual(record.worked_time_display, "8:30")

    def test_admin_cannot_edit_user_outside_the_group(self):
        outsider = get_user_model().objects.create(
            username="outsider", username_jp="外部", email="outsider@example.com", is_active=True,
        )
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("shift:attendance_summary", args=[self.group.id]),
            {
                "month": self.month_value,
                "user_id": str(outsider.id),
                "work_date": self.today.isoformat(),
                "start_time": "10:00",
                "end_time": "19:00",
                "break_minutes": "0",
                "note": "",
            },
        )

        self.assertFalse(AttendanceRecord.objects.filter(user=outsider).exists())

    # ----- 実働時間の計算 -----

    def test_worked_minutes_handles_break_and_overnight_shift(self):
        day_shift = AttendanceRecord(start_time=time(9, 0), end_time=time(18, 0), break_minutes=60)
        night_shift = AttendanceRecord(start_time=time(22, 0), end_time=time(6, 30), break_minutes=30)
        working = AttendanceRecord(start_time=time(9, 0))

        self.assertEqual(day_shift.worked_minutes, 480)
        self.assertEqual(night_shift.worked_minutes, 480)  # 22:00〜翌6:30 から休憩30分
        self.assertTrue(night_shift.is_overnight)
        self.assertIsNone(working.worked_minutes)
        self.assertTrue(working.is_working)


class SlackNotificationTests(TestCase):
    """グループごとの Slack 通知（設定・手動送信・自動通知）"""

    WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/xxxxxxxx"

    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create(
            username="slack-member", username_jp="メンバー", email="slack-member@example.com",
            is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="slack-admin", username_jp="管理者", email="slack-admin@example.com",
            is_active=True,
        )
        self.group = Group.objects.create(name="slack-shop", name_jp="通知店舗")
        UserGroup.objects.create(user=self.member, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.time_slot = TimeSlot.objects.create(
            group=self.group, name="早番", start_time=time(9, 0), end_time=time(13, 0),
        )
        self.url = reverse("shift:slack_settings", args=[self.group.id])

    def _enable_slack(self, **overrides):
        defaults = {"is_enabled": True, "webhook_url": self.WEBHOOK_URL}
        defaults.update(overrides)
        setting, _created = SlackNotificationSetting.objects.update_or_create(
            group=self.group, defaults=defaults,
        )
        return setting

    def test_settings_page_requires_admin(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url).status_code, 403)

        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_admin_can_save_webhook_url(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(self.url, {
            "action": "save",
            "is_enabled": "on",
            "webhook_url": self.WEBHOOK_URL,
            "notify_deadline_reminder": "on",
            "reminder_days_before": "3",
        })

        self.assertEqual(response.status_code, 302)
        setting = SlackNotificationSetting.objects.get(group=self.group)
        self.assertTrue(setting.is_ready)
        self.assertEqual(setting.webhook_url, self.WEBHOOK_URL)

    def test_enabling_without_webhook_url_is_rejected(self):
        self.client.force_login(self.admin_user)

        self.client.post(self.url, {
            "action": "save", "is_enabled": "on", "webhook_url": "", "reminder_days_before": "3",
        })

        self.assertFalse(SlackNotificationSetting.objects.get(group=self.group).is_enabled)

    def test_invalid_save_redisplays_the_stored_state(self):
        self._enable_slack()
        self.client.force_login(self.admin_user)

        response = self.client.post(self.url, {
            "action": "save", "is_enabled": "on",
            "webhook_url": "https://example.com/hook", "reminder_days_before": "3",
        })

        # 入力エラーで差し戻されても、状態表示は保存済みの内容のまま
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["setting"].is_ready)
        self.assertContains(response, "送信できます")

    def test_non_slack_webhook_url_is_rejected(self):
        self.client.force_login(self.admin_user)

        self.client.post(self.url, {
            "action": "save", "is_enabled": "on",
            "webhook_url": "https://example.com/hook", "reminder_days_before": "3",
        })

        self.assertEqual(SlackNotificationSetting.objects.get(group=self.group).webhook_url, "")

    def test_blank_webhook_url_keeps_the_saved_one(self):
        self._enable_slack()
        self.client.force_login(self.admin_user)

        self.client.post(self.url, {
            "action": "save", "is_enabled": "on", "webhook_url": "",
            "mention": "<!here>", "reminder_days_before": "5",
        })

        setting = SlackNotificationSetting.objects.get(group=self.group)
        self.assertEqual(setting.webhook_url, self.WEBHOOK_URL)
        self.assertEqual(setting.mention, "<!here>")
        self.assertEqual(setting.reminder_days_before, 5)

    def test_saved_webhook_url_is_not_rendered_in_the_form(self):
        self._enable_slack()
        self.client.force_login(self.admin_user)

        response = self.client.get(self.url)

        self.assertNotContains(response, self.WEBHOOK_URL)
        self.assertContains(response, "登録済み")

    def test_clear_checkbox_removes_webhook_url(self):
        self._enable_slack()
        self.client.force_login(self.admin_user)

        self.client.post(self.url, {
            "action": "save", "webhook_url": "", "clear_webhook_url": "on", "reminder_days_before": "3",
        })

        setting = SlackNotificationSetting.objects.get(group=self.group)
        self.assertEqual(setting.webhook_url, "")
        self.assertFalse(setting.is_ready)

    def test_manual_request_sends_message_with_unsubmitted_members(self):
        self._enable_slack(mention="<!here>")
        self.client.force_login(self.admin_user)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.return_value = mock.Mock(status_code=200, body="ok")
            response = self.client.post(self.url, {
                "action": "request",
                "period_start": "2026-06-01",
                "period_end": "2026-06-30",
                "deadline_date": "2026-05-25",
                "message": "早めにお願いします",
                "include_unsubmitted": "on",
            })

        self.assertEqual(response.status_code, 302)
        webhook_client.assert_called_once_with(self.WEBHOOK_URL, timeout=slack.SEND_TIMEOUT_SECONDS)
        text = webhook_client.return_value.send.call_args.kwargs["attachments"][0]["text"]
        self.assertIn("<!here>", text)
        self.assertIn("2026/06/01", text)
        self.assertIn("2026/05/25", text)
        self.assertIn("早めにお願いします", text)
        self.assertIn("slack-member", text)  # 未提出メンバーの表示名

    def test_manual_request_is_blocked_when_slack_is_disabled(self):
        self.client.force_login(self.admin_user)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            self.client.post(self.url, {
                "action": "request", "period_start": "2026-06-01", "period_end": "2026-06-30",
            })

        webhook_client.assert_not_called()

    def test_confirm_notifies_when_enabled(self):
        self._enable_slack(notify_shift_confirmed=True)
        today = timezone.now().date()
        ShiftEntry.objects.create(
            group=self.group, user=self.member, work_date=today,
            time_slot=self.time_slot, status=ShiftEntry.AVAILABLE, is_draft=True,
        )
        self.client.force_login(self.member)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.return_value = mock.Mock(status_code=200, body="ok")
            self.client.post(reverse("shift:shift_confirm", args=[self.group.id]), {
                "date_from": today.isoformat(), "date_to": today.isoformat(),
            })

        self.assertEqual(webhook_client.return_value.send.call_count, 1)
        text = webhook_client.return_value.send.call_args.kwargs["attachments"][0]["text"]
        self.assertIn("slack-member", text)

    def test_confirm_does_not_notify_when_toggle_is_off(self):
        self._enable_slack(notify_shift_confirmed=False)
        today = timezone.now().date()
        ShiftEntry.objects.create(
            group=self.group, user=self.member, work_date=today,
            time_slot=self.time_slot, status=ShiftEntry.AVAILABLE, is_draft=True,
        )
        self.client.force_login(self.member)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            self.client.post(reverse("shift:shift_confirm", args=[self.group.id]), {
                "date_from": today.isoformat(), "date_to": today.isoformat(),
            })

        webhook_client.assert_not_called()

    def test_deadline_save_notifies_and_resets_reminder(self):
        self._enable_slack(notify_schedule_changed=True)
        self.client.force_login(self.admin_user)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.return_value = mock.Mock(status_code=200, body="ok")
            self.client.post(reverse("shift:schedule_settings", args=[self.group.id]), {
                "action": "deadline",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "deadline_date": "2026-06-25",
                "note": "",
            })

        deadline = ShiftDeadline.objects.get(group=self.group, period_start=date(2026, 7, 1))
        self.assertIsNone(deadline.reminder_sent_at)
        self.assertEqual(webhook_client.return_value.send.call_count, 1)

    def test_schedule_change_notifies(self):
        self._enable_slack(notify_schedule_changed=True)
        schedule_type = OpeningScheduleType.objects.create(group=self.group, name="休校",
                                                           blocks_shift_input=True)
        self.client.force_login(self.admin_user)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.return_value = mock.Mock(status_code=200, body="ok")
            self.client.post(reverse("shift:schedule_settings", args=[self.group.id]), {
                "action": "schedule_date",
                "work_date": "2026-07-10",
                "schedule_type": str(schedule_type.id),
                "note": "",
            })

        text = webhook_client.return_value.send.call_args.kwargs["attachments"][0]["text"]
        self.assertIn("休校", text)
        self.assertIn("7/10", text)

    def test_send_failure_does_not_break_the_page(self):
        self._enable_slack()
        self.client.force_login(self.admin_user)

        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.side_effect = RuntimeError("boom")
            response = self.client.post(self.url, {
                "action": "test",
            }, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Slackへの送信に失敗しました")


class SlackReminderCommandTests(TestCase):
    """send_shift_reminders コマンド"""

    WEBHOOK_URL = "https://hooks.slack.com/services/T000/B000/yyyyyyyy"

    def setUp(self):
        user_model = get_user_model()
        self.member = user_model.objects.create(
            username="reminder-member", username_jp="メンバー",
            email="reminder-member@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="reminder-shop", name_jp="リマインド店舗")
        UserGroup.objects.create(user=self.member, group=self.group)
        self.setting = SlackNotificationSetting.objects.create(
            group=self.group, is_enabled=True, webhook_url=self.WEBHOOK_URL,
            notify_deadline_reminder=True, reminder_days_before=3,
        )

    def _deadline(self, days_ahead):
        # (group, period_start) が一意なので、期限ごとに対象期間もずらす
        today = timezone.now().date()
        period_start = today + timedelta(days=days_ahead)
        return ShiftDeadline.objects.create(
            group=self.group,
            period_start=period_start,
            period_end=period_start + timedelta(days=30),
            deadline_date=today + timedelta(days=days_ahead),
        )

    def _run(self, **options):
        with mock.patch("shift.slack.WebhookClient") as webhook_client:
            webhook_client.return_value.send.return_value = mock.Mock(status_code=200, body="ok")
            call_command("send_shift_reminders", stdout=StringIO(), stderr=StringIO(), **options)
        return webhook_client

    def test_sends_only_within_the_configured_window(self):
        near = self._deadline(2)
        far = self._deadline(30)

        webhook_client = self._run()

        self.assertEqual(webhook_client.return_value.send.call_count, 1)
        near.refresh_from_db()
        far.refresh_from_db()
        self.assertIsNotNone(near.reminder_sent_at)
        self.assertIsNone(far.reminder_sent_at)

    def test_does_not_send_twice_for_the_same_deadline(self):
        self._deadline(1)

        self._run()
        webhook_client = self._run()

        webhook_client.return_value.send.assert_not_called()

    def test_dry_run_does_not_send(self):
        deadline = self._deadline(1)

        webhook_client = self._run(dry_run=True)

        webhook_client.return_value.send.assert_not_called()
        deadline.refresh_from_db()
        self.assertIsNone(deadline.reminder_sent_at)

    def test_skips_disabled_groups(self):
        self.setting.is_enabled = False
        self.setting.save()
        self._deadline(1)

        webhook_client = self._run()

        webhook_client.return_value.send.assert_not_called()
