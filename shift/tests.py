from datetime import date, time, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from django.urls import reverse

from custom_auth.models import Group, UserGroup
from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
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
