import csv
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
    WorkType,
    find_overlapping_record,
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
        record = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(18, 0),
        )
        self.client.force_login(self.user)

        self.client.post(
            self._attendance_url(),
            {
                "month": self.month_value,
                "work_date": self.today.isoformat(),
                "record_id": str(record.id),
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


class AttendanceMultipleRecordTests(TestCase):
    """同じ日に複数の勤怠を登録する（手動入力・打刻・管理者の修正）"""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="user", username_jp="ユーザー", email="user@example.com", is_active=True,
        )
        self.other_user = user_model.objects.create(
            username="other", username_jp="他メンバー", email="other@example.com", is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin", username_jp="管理者", email="admin@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        UserGroup.objects.create(user=self.user, group=self.group)
        UserGroup.objects.create(user=self.other_user, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        self.setting = AttendanceSetting.objects.create(group=self.group, is_enabled=True)
        self.today = timezone.now().date()
        self.month_value = self.today.strftime("%Y-%m")
        self.url = reverse("shift:attendance", args=[self.group.id])
        self.clock_url = reverse("shift:attendance_clock", args=[self.group.id])

    def _post_record(self, **values):
        data = {
            "month": self.month_value,
            "work_date": self.today.isoformat(),
            "start_time": "",
            "end_time": "",
            "break_minutes": "",
            "note": "",
        }
        data.update(values)
        return self.client.post(self.url, data)

    def _create(self, start, end=None, user=None, **kwargs):
        return AttendanceRecord.objects.create(
            group=self.group, user=user or self.user, work_date=self.today,
            start_time=start, end_time=end, **kwargs,
        )

    def _now(self, hour, minute=0):
        return timezone.datetime.combine(self.today, time(hour, minute))

    # ----- 手動入力 -----

    def test_manual_input_adds_second_record_on_the_same_day(self):
        self._create(time(9, 0), time(12, 0))
        self.client.force_login(self.user)

        self._post_record(start_time="13:00", end_time="17:00")

        records = AttendanceRecord.objects.filter(group=self.group, user=self.user, work_date=self.today)
        self.assertEqual(
            [(r.start_time, r.end_time) for r in records],
            [(time(9, 0), time(12, 0)), (time(13, 0), time(17, 0))],
        )

    def test_manual_input_rejects_overlapping_record(self):
        self._create(time(9, 0), time(12, 0))
        self.client.force_login(self.user)

        response = self._post_record(start_time="11:00", end_time="13:00")

        self.assertEqual(AttendanceRecord.objects.filter(user=self.user).count(), 1)
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any("時間が重なっています" in message for message in messages))

    def test_double_submission_does_not_create_duplicates(self):
        self.client.force_login(self.user)

        self._post_record(start_time="09:00", end_time="12:00")
        self._post_record(start_time="09:00", end_time="12:00")

        self.assertEqual(AttendanceRecord.objects.filter(user=self.user).count(), 1)

    def test_manual_input_edits_only_the_selected_record(self):
        morning = self._create(time(9, 0), time(12, 0))
        afternoon = self._create(time(13, 0), time(17, 0))
        self.client.force_login(self.user)

        self._post_record(record_id=str(afternoon.id), start_time="13:30", end_time="17:00")

        morning.refresh_from_db()
        afternoon.refresh_from_db()
        self.assertEqual(morning.start_time, time(9, 0))
        self.assertEqual(afternoon.start_time, time(13, 30))

    def test_editing_a_record_ignores_itself_in_the_overlap_check(self):
        record = self._create(time(9, 0), time(12, 0))
        self.client.force_login(self.user)

        self._post_record(record_id=str(record.id), start_time="09:00", end_time="12:30")

        record.refresh_from_db()
        self.assertEqual(record.end_time, time(12, 30))

    def test_delete_action_removes_only_the_selected_record(self):
        morning = self._create(time(9, 0), time(12, 0))
        afternoon = self._create(time(13, 0), time(17, 0))
        self.client.force_login(self.user)

        self._post_record(record_id=str(morning.id), action="delete")

        self.assertFalse(AttendanceRecord.objects.filter(id=morning.id).exists())
        self.assertTrue(AttendanceRecord.objects.filter(id=afternoon.id).exists())

    def test_member_cannot_edit_or_delete_another_members_record(self):
        record = self._create(time(9, 0), time(12, 0), user=self.other_user)
        self.client.force_login(self.user)

        self._post_record(record_id=str(record.id), start_time="10:00", end_time="12:00")
        self._post_record(record_id=str(record.id), action="delete")

        record.refresh_from_db()
        self.assertEqual(record.start_time, time(9, 0))
        self.assertFalse(AttendanceRecord.objects.filter(user=self.user).exists())

    def test_invalid_date_is_rejected_without_error(self):
        self.client.force_login(self.user)

        response = self._post_record(work_date="2026-02-30", start_time="09:00", end_time="12:00")

        self.assertEqual(response.status_code, 302)
        self.assertFalse(AttendanceRecord.objects.filter(user=self.user).exists())

    def test_attendance_page_lists_every_record_of_the_day(self):
        self._create(time(9, 0), time(12, 0))
        self._create(time(13, 0), time(17, 0), break_minutes=30)
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertContains(response, 'data-record-id="', count=2)
        self.assertContains(response, "勤務日数 1 日")
        self.assertContains(response, "実働合計 6:30")

    def test_attendance_page_shows_add_entry_points_only_while_manual_input_is_allowed(self):
        self._create(time(9, 0), time(12, 0))
        self.client.force_login(self.user)

        allowed = self.client.get(self.url)
        self.setting.allow_manual_input = False
        self.setting.save()
        disallowed = self.client.get(self.url)

        self.assertContains(allowed, 'class="attendance-add-row', count=1)
        self.assertContains(allowed, 'id="attendanceRecordsByDate"')
        self.assertEqual(
            [record["start"] for record in allowed.context["records_payload"][self.today.isoformat()]],
            ["09:00"],
        )
        self.assertNotContains(disallowed, 'class="attendance-add-row')
        self.assertNotContains(disallowed, 'data-attendance-action="add"')

    # ----- 打刻 -----

    def test_clock_in_again_after_clock_out_creates_another_record(self):
        self.client.force_login(self.user)

        with mock.patch("django.utils.timezone.now", return_value=self._now(9)):
            self.client.post(self.clock_url, {"action": "in"})
        with mock.patch("django.utils.timezone.now", return_value=self._now(12)):
            self.client.post(self.clock_url, {"action": "out"})
        with mock.patch("django.utils.timezone.now", return_value=self._now(13)):
            self.client.post(self.clock_url, {"action": "in"})
        with mock.patch("django.utils.timezone.now", return_value=self._now(17)):
            self.client.post(self.clock_url, {"action": "out"})

        records = AttendanceRecord.objects.filter(user=self.user, work_date=self.today)
        self.assertEqual(
            [(r.start_time, r.end_time) for r in records],
            [(time(9, 0), time(12, 0)), (time(13, 0), time(17, 0))],
        )

    def test_clock_in_while_working_keeps_the_first_time(self):
        self.client.force_login(self.user)

        with mock.patch("django.utils.timezone.now", return_value=self._now(9)):
            self.client.post(self.clock_url, {"action": "in"})
        with mock.patch("django.utils.timezone.now", return_value=self._now(10)):
            self.client.post(self.clock_url, {"action": "in"})

        record = AttendanceRecord.objects.get(user=self.user, work_date=self.today)
        self.assertEqual(record.start_time, time(9, 0))

    def test_clock_in_inside_a_registered_record_is_rejected(self):
        self._create(time(9, 0), time(12, 0))
        self.client.force_login(self.user)

        with mock.patch("django.utils.timezone.now", return_value=self._now(10)):
            self.client.post(self.clock_url, {"action": "in"})

        self.assertEqual(AttendanceRecord.objects.filter(user=self.user).count(), 1)

    # ----- 管理者の修正 -----

    def test_admin_can_add_and_edit_records_of_a_member(self):
        record = self._create(time(9, 0), time(12, 0))
        summary_url = reverse("shift:attendance_summary", args=[self.group.id])
        self.client.force_login(self.admin_user)
        base = {
            "month": self.month_value, "user_id": str(self.user.id), "work_date": self.today.isoformat(),
            "break_minutes": "", "note": "",
        }

        self.client.post(summary_url, {**base, "start_time": "13:00", "end_time": "15:00"})
        self.client.post(summary_url, {**base, "record_id": str(record.id), "start_time": "08:30", "end_time": "12:00"})

        records = AttendanceRecord.objects.filter(user=self.user, work_date=self.today)
        self.assertEqual(
            [(r.start_time, r.end_time) for r in records],
            [(time(8, 30), time(12, 0)), (time(13, 0), time(15, 0))],
        )

    def test_admin_cannot_edit_record_through_another_member(self):
        record = self._create(time(9, 0), time(12, 0), user=self.other_user)
        self.client.force_login(self.admin_user)

        self.client.post(
            reverse("shift:attendance_summary", args=[self.group.id]),
            {
                "month": self.month_value, "user_id": str(self.user.id), "record_id": str(record.id),
                "work_date": self.today.isoformat(), "start_time": "10:00", "end_time": "12:00",
                "break_minutes": "", "note": "",
            },
        )

        record.refresh_from_db()
        self.assertEqual(record.start_time, time(9, 0))
        self.assertEqual(record.user, self.other_user)

    def test_summary_sums_records_of_the_same_day(self):
        self._create(time(9, 0), time(12, 0))
        self._create(time(13, 0), time(17, 0))
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("shift:attendance_summary", args=[self.group.id]))

        self.assertContains(response, "7:00<sup")
        self.assertContains(response, "×2")

    # ----- 時間帯の重なり -----

    def test_find_overlapping_record(self):
        morning = AttendanceRecord(start_time=time(9, 0), end_time=time(12, 0))
        night = AttendanceRecord(start_time=time(22, 0), end_time=time(2, 0))
        working = AttendanceRecord(start_time=time(14, 0))
        records = [morning, night, working]

        self.assertIs(find_overlapping_record(records, time(11, 0), time(13, 0)), morning)
        self.assertIsNone(find_overlapping_record(records, time(12, 0), time(13, 0)))  # 境目は重ならない
        self.assertIs(find_overlapping_record(records, time(23, 0), None), night)
        self.assertIs(find_overlapping_record(records, time(13, 0), time(15, 0)), working)
        self.assertIsNone(find_overlapping_record(records, time(15, 0), time(16, 0)))
        self.assertIsNone(find_overlapping_record(records, None, None))


class WorkTypeTests(TestCase):
    """勤怠の仕事内容と時給・金額の計算"""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="user", username_jp="ユーザー", email="user@example.com", is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin", username_jp="管理者", email="admin@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.user, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        AttendanceSetting.objects.create(group=self.group, is_enabled=True)
        self.lesson = WorkType.objects.create(group=self.group, name="授業", hourly_rate=1500, display_order=1)
        self.office = WorkType.objects.create(group=self.group, name="事務", hourly_rate=1100, display_order=2)
        self.today = timezone.now().date()
        self.month_value = self.today.strftime("%Y-%m")
        self.url = reverse("shift:attendance", args=[self.group.id])
        self.settings_url = reverse("shift:attendance_settings", args=[self.group.id])

    def _post_record(self, **values):
        data = {
            "month": self.month_value,
            "work_date": self.today.isoformat(),
            "start_time": "",
            "end_time": "",
            "break_minutes": "",
            "note": "",
        }
        data.update(values)
        return self.client.post(self.url, data)

    def _post_work_type(self, **values):
        data = {"action": "work_type", "name": "", "hourly_rate": "", "display_order": "100", "is_active": "on"}
        data.update(values)
        return self.client.post(self.settings_url, data)

    # ----- 金額の計算 -----

    def test_amount_is_worked_minutes_times_hourly_rate(self):
        record = AttendanceRecord(start_time=time(9, 0), end_time=time(10, 30), hourly_rate=1000)
        short = AttendanceRecord(start_time=time(9, 0), end_time=time(9, 1), hourly_rate=1000)
        no_rate = AttendanceRecord(start_time=time(9, 0), end_time=time(10, 0))
        working = AttendanceRecord(start_time=time(9, 0), hourly_rate=1000)

        self.assertEqual(record.amount, 1500)
        self.assertEqual(record.amount_display, "1,500円")
        self.assertEqual(short.amount, 17)  # 16.66… 円は四捨五入
        self.assertIsNone(no_rate.amount)
        self.assertIsNone(working.amount)

    # ----- 勤怠の登録 -----

    def test_manual_input_saves_work_type_and_its_rate(self):
        self.client.force_login(self.user)

        self._post_record(work_type=str(self.lesson.id), start_time="09:00", end_time="11:00")

        record = AttendanceRecord.objects.get(user=self.user)
        self.assertEqual(record.work_type, self.lesson)
        self.assertEqual(record.hourly_rate, 1500)
        self.assertEqual(record.amount, 3000)

    def test_work_type_is_required_when_the_group_has_work_types(self):
        self.client.force_login(self.user)

        response = self._post_record(start_time="09:00", end_time="11:00")

        self.assertFalse(AttendanceRecord.objects.filter(user=self.user).exists())
        messages = [str(message) for message in response.wsgi_request._messages]
        self.assertTrue(any("仕事内容を選択してください" in message for message in messages))

    def test_work_type_of_another_group_is_rejected(self):
        foreign = WorkType.objects.create(group=self.other_group, name="授業", hourly_rate=9999)
        self.client.force_login(self.user)

        self._post_record(work_type=str(foreign.id), start_time="09:00", end_time="11:00")

        self.assertFalse(AttendanceRecord.objects.filter(user=self.user).exists())

    def test_inactive_work_type_cannot_be_chosen_but_existing_record_can_be_edited(self):
        record = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.office, hourly_rate=1100,
        )
        self.office.is_active = False
        self.office.save()
        self.client.force_login(self.user)

        self._post_record(work_type=str(self.office.id), start_time="13:00", end_time="14:00")
        self._post_record(record_id=str(record.id), work_type=str(self.office.id),
                          start_time="09:00", end_time="10:30")

        self.assertEqual(AttendanceRecord.objects.filter(user=self.user).count(), 1)
        record.refresh_from_db()
        self.assertEqual(record.end_time, time(10, 30))
        self.assertEqual(record.work_type, self.office)

    def test_clock_in_saves_selected_work_type(self):
        self.client.force_login(self.user)

        self.client.post(
            reverse("shift:attendance_clock", args=[self.group.id]),
            {"action": "in", "work_type": str(self.office.id)},
        )

        record = AttendanceRecord.objects.get(user=self.user)
        self.assertEqual(record.work_type, self.office)
        self.assertEqual(record.hourly_rate, 1100)

    def test_clock_in_without_work_type_is_rejected(self):
        self.client.force_login(self.user)

        self.client.post(reverse("shift:attendance_clock", args=[self.group.id]), {"action": "in"})

        self.assertFalse(AttendanceRecord.objects.filter(user=self.user).exists())

    # ----- 時給の変更 -----

    def test_rate_change_does_not_affect_registered_records(self):
        record = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.lesson, hourly_rate=1500,
        )
        self.client.force_login(self.admin_user)

        self._post_work_type(work_type_id=str(self.lesson.id), name="授業", hourly_rate="1600")

        self.lesson.refresh_from_db()
        record.refresh_from_db()
        self.assertEqual(self.lesson.hourly_rate, 1600)
        self.assertEqual(record.hourly_rate, 1500)

    def test_rate_change_can_be_applied_from_a_date(self):
        old = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today - timedelta(days=40),
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.lesson, hourly_rate=1500,
        )
        new = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.lesson, hourly_rate=1500,
        )
        other_type = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(13, 0), end_time=time(14, 0), work_type=self.office, hourly_rate=1100,
        )
        self.client.force_login(self.admin_user)

        self._post_work_type(
            work_type_id=str(self.lesson.id), name="授業", hourly_rate="1600",
            apply_from=(self.today - timedelta(days=7)).isoformat(),
        )

        old.refresh_from_db()
        new.refresh_from_db()
        other_type.refresh_from_db()
        self.assertEqual(old.hourly_rate, 1500)
        self.assertEqual(new.hourly_rate, 1600)
        self.assertEqual(other_type.hourly_rate, 1100)

    def test_editing_a_record_keeps_its_rate_unless_the_work_type_changes(self):
        record = AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.lesson, hourly_rate=1400,
        )
        self.client.force_login(self.user)

        self._post_record(record_id=str(record.id), work_type=str(self.lesson.id),
                          start_time="09:00", end_time="10:00", note="メモだけ変更")
        record.refresh_from_db()
        self.assertEqual(record.hourly_rate, 1400)

        self._post_record(record_id=str(record.id), work_type=str(self.office.id),
                          start_time="09:00", end_time="10:00")
        record.refresh_from_db()
        self.assertEqual(record.hourly_rate, 1100)

    # ----- 表示 -----

    def test_attendance_page_shows_amounts_by_work_type(self):
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(11, 0), work_type=self.lesson, hourly_rate=1500,
        )
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(13, 0), end_time=time(14, 30), work_type=self.office, hourly_rate=1100,
        )
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertContains(response, "金額合計 4,650円")
        self.assertContains(response, "3,000円")
        self.assertContains(response, "1,650円")

    def test_page_without_work_types_hides_amounts(self):
        WorkType.objects.all().delete()
        self.client.force_login(self.user)

        response = self.client.get(self.url)

        self.assertNotContains(response, "金額合計")

    def test_summary_shows_totals_and_breakdown(self):
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(11, 0), work_type=self.lesson, hourly_rate=1500,
        )
        AttendanceRecord.objects.create(
            group=self.group, user=self.admin_user, work_date=self.today,
            start_time=time(13, 0), end_time=time(14, 0), work_type=self.office, hourly_rate=1100,
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("shift:attendance_summary", args=[self.group.id]))

        self.assertContains(response, "金額合計 4,100円")
        self.assertContains(response, "仕事内容別の集計")
        breakdown = response.context["breakdown"]
        self.assertEqual(breakdown["columns"], [self.lesson, self.office])
        self.assertEqual([item["amount_display"] for item in breakdown["footer"]], ["3,000円", "1,100円"])

    # ----- 仕事内容の管理 -----

    def test_member_cannot_manage_work_types(self):
        self.client.force_login(self.user)

        response = self._post_work_type(name="清掃", hourly_rate="1000")

        self.assertEqual(response.status_code, 403)
        self.assertFalse(WorkType.objects.filter(name="清掃").exists())

    def test_admin_can_add_work_type(self):
        self.client.force_login(self.admin_user)

        response = self._post_work_type(name="清掃", hourly_rate="1050")

        self.assertRedirects(response, self.settings_url)
        work_type = WorkType.objects.get(group=self.group, name="清掃")
        self.assertEqual(work_type.hourly_rate, 1050)
        self.assertTrue(work_type.is_active)

    def test_duplicate_work_type_name_is_rejected(self):
        self.client.force_login(self.admin_user)

        self._post_work_type(name="授業", hourly_rate="2000")

        self.assertEqual(WorkType.objects.filter(group=self.group, name="授業").count(), 1)

    def test_work_type_of_another_group_is_not_editable(self):
        foreign = WorkType.objects.create(group=self.other_group, name="外部", hourly_rate=1000)
        self.client.force_login(self.admin_user)

        self._post_work_type(work_type_id=str(foreign.id), name="乗っ取り", hourly_rate="1")

        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "外部")

    def test_unused_work_type_can_be_deleted_but_used_one_cannot(self):
        AttendanceRecord.objects.create(
            group=self.group, user=self.user, work_date=self.today,
            start_time=time(9, 0), end_time=time(10, 0), work_type=self.lesson, hourly_rate=1500,
        )
        self.client.force_login(self.admin_user)

        self.client.post(reverse("shift:work_type_delete", args=[self.group.id, self.lesson.id]))
        self.client.post(reverse("shift:work_type_delete", args=[self.group.id, self.office.id]))

        self.assertTrue(WorkType.objects.filter(id=self.lesson.id).exists())
        self.assertFalse(WorkType.objects.filter(id=self.office.id).exists())


class AttendanceCsvTests(TestCase):
    """勤怠集計の CSV ダウンロード"""

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create(
            username="user", username_jp="山田 花子", email="user@example.com", is_active=True,
        )
        self.admin_user = user_model.objects.create(
            username="admin", username_jp="管理 太郎", email="admin@example.com", is_active=True,
        )
        self.outsider = user_model.objects.create(
            username="outsider", username_jp="外部", email="outsider@example.com", is_active=True,
        )
        self.group = Group.objects.create(name="shop", name_jp="店舗")
        self.other_group = Group.objects.create(name="other-shop", name_jp="別店舗")
        UserGroup.objects.create(user=self.user, group=self.group)
        UserGroup.objects.create(user=self.admin_user, group=self.group, is_admin=True)
        UserGroup.objects.create(user=self.outsider, group=self.other_group)
        AttendanceSetting.objects.create(group=self.group, is_enabled=True)
        self.lesson = WorkType.objects.create(group=self.group, name="授業", hourly_rate=1500, display_order=1)
        self.office = WorkType.objects.create(group=self.group, name="事務", hourly_rate=1100, display_order=2)
        self.month_first = timezone.now().date().replace(day=1)
        self.url = reverse("shift:attendance_summary_csv", args=[self.group.id])

    def _create(self, user, day, start, end, work_type=None, group=None, **kwargs):
        record = AttendanceRecord(
            group=group or self.group, user=user, work_date=day, start_time=start, end_time=end, **kwargs,
        )
        record.apply_work_type(work_type)
        record.save()
        return record

    def _download(self, kind, month=None):
        response = self.client.get(self.url, {"kind": kind, "month": (month or self.month_first).strftime("%Y-%m")})
        content = response.content.decode("utf-8")
        return response, list(csv.reader(content.lstrip("\ufeff").splitlines()))

    def test_csv_requires_group_admin(self):
        self.client.force_login(self.user)

        response = self.client.get(self.url, {"kind": "records"})

        self.assertEqual(response.status_code, 403)

    def test_unknown_kind_is_not_found(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(self.url, {"kind": "secret"})

        self.assertEqual(response.status_code, 404)

    def test_records_csv_lists_each_record_of_the_month(self):
        self._create(self.user, self.month_first, time(9, 0), time(12, 0), self.lesson, note="午前")
        self._create(self.user, self.month_first, time(22, 0), time(1, 30), self.office, break_minutes=30)
        self._create(self.user, self.month_first - timedelta(days=1), time(9, 0), time(10, 0), self.lesson)
        self._create(self.outsider, self.month_first, time(9, 0), time(10, 0), group=self.other_group)
        self.client.force_login(self.admin_user)

        response, rows = self._download("records")

        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertTrue(response.content.startswith("\ufeff".encode("utf-8")))
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn(self.month_first.strftime("%Y-%m"), response["Content-Disposition"])
        self.assertEqual(rows[0], [
            "勤務日", "曜日", "ユーザー名", "氏名", "仕事内容", "出勤時刻", "退勤時刻", "日またぎ",
            "休憩（分）", "実働（時:分）", "実働（分）", "時給（円）", "金額（円）", "入力方法", "メモ",
        ])
        self.assertEqual(len(rows), 3)
        day = self.month_first.isoformat()
        self.assertEqual(rows[1][0], day)
        self.assertEqual(rows[1][2:], [
            "user", "山田 花子", "授業", "09:00", "12:00", "", "0", "3:00", "180", "1500", "4500", "手動入力", "午前",
        ])
        self.assertEqual(rows[2][4:13], ["事務", "22:00", "01:30", "○", "30", "3:00", "180", "1100", "3300"])

    def test_records_csv_leaves_working_record_blank(self):
        self._create(self.user, self.month_first, time(9, 0), None, self.lesson)
        self.client.force_login(self.admin_user)

        _response, rows = self._download("records")

        self.assertEqual(rows[1][6], "")
        self.assertEqual(rows[1][9:13], ["", "", "1500", ""])

    def test_members_csv_has_totals_and_work_type_columns(self):
        self._create(self.user, self.month_first, time(9, 0), time(11, 0), self.lesson)
        self._create(self.user, self.month_first, time(13, 0), time(14, 30), self.office)
        self._create(self.user, self.month_first + timedelta(days=1), time(9, 0), time(10, 0), self.lesson)
        self.client.force_login(self.admin_user)

        _response, rows = self._download("members")

        self.assertEqual(rows[0], [
            "ユーザー名", "氏名", "勤務日数", "実働（時:分）", "実働（分）", "金額（円）",
            "授業 実働（分）", "授業 金額（円）", "事務 実働（分）", "事務 金額（円）",
        ])
        self.assertEqual(rows[1], ["admin", "管理 太郎", "0", "0:00", "0", "0", "0", "0", "0", "0"])
        self.assertEqual(rows[2], ["user", "山田 花子", "2", "4:30", "270", "6150", "180", "4500", "90", "1650"])

    def test_csv_without_work_types_has_no_amount_columns(self):
        WorkType.objects.all().delete()
        self._create(self.user, self.month_first, time(9, 0), time(12, 0))
        self.client.force_login(self.admin_user)

        _response, record_rows = self._download("records")
        _response, member_rows = self._download("members")

        self.assertNotIn("金額（円）", record_rows[0])
        self.assertEqual(member_rows[0], ["ユーザー名", "氏名", "勤務日数", "実働（時:分）", "実働（分）"])

    def test_csv_escapes_text_that_looks_like_a_formula(self):
        self._create(self.user, self.month_first, time(9, 0), time(12, 0), self.lesson, note="=HYPERLINK(1)")
        self.client.force_login(self.admin_user)

        _response, rows = self._download("records")

        self.assertEqual(rows[1][-1], "'=HYPERLINK(1)")

    def test_summary_page_links_to_csv(self):
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse("shift:attendance_summary", args=[self.group.id]))

        self.assertContains(response, f"{self.url}?month={self.month_first:%Y-%m}&amp;kind=records")
        self.assertContains(response, f"{self.url}?month={self.month_first:%Y-%m}&amp;kind=members")


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
