import json
import calendar as cal_module
from datetime import date as date_type, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from custom_auth.models import Group, UserGroup
from shift.forms import (
    OpeningScheduleTypeForm,
    ShiftDeadlineForm,
)
from shift.models import (
    AttendanceSetting,
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
    TimeSlot,
    WEEKDAY_CHOICES,
)


ACTIVE_GROUP_STATUS = 1


def _today():
    return timezone.now().date()


def _parse_date_or_default(value, default):
    if not value:
        return default
    return parse_date(value) or default


def _active_groups():
    return Group.objects.filter(status=ACTIVE_GROUP_STATUS).order_by("name")


def _user_memberships(user):
    return (
        UserGroup.objects.filter(user=user, group__status=ACTIVE_GROUP_STATUS)
        .select_related("group")
        .order_by("group__name")
    )


def _admin_groups_for_user(user):
    if user.is_staff:
        return list(_active_groups())

    return [
        membership.group
        for membership in (
            UserGroup.objects.filter(user=user, is_admin=True, group__status=ACTIVE_GROUP_STATUS)
            .select_related("group")
            .order_by("group__name")
        )
    ]


def _get_group_for_user(request, group_id, require_admin=False):
    group = get_object_or_404(Group, id=group_id, status=ACTIVE_GROUP_STATUS)
    if request.user.is_staff:
        return group

    membership = UserGroup.objects.filter(user=request.user, group=group).first()
    if membership is None:
        raise PermissionDenied
    if require_admin and not membership.is_admin:
        raise PermissionDenied
    return group


def _active_time_slots():
    return list(TimeSlot.objects.filter(is_active=True).order_by("start_time"))


def _date_range(date_from, date_to):
    days = (date_to - date_from).days + 1
    for offset in range(days):
        yield date_from + timedelta(days=offset)


def _weekday_label(weekday):
    return dict(WEEKDAY_CHOICES)[weekday]


def _ensure_default_opening_schedule_types(group):
    defaults = (
        ("開講", False, 10),
        ("休校", True, 20),
        ("自習室", False, 30),
    )
    for name, blocks_shift_input, display_order in defaults:
        OpeningScheduleType.objects.get_or_create(
            group=group,
            name=name,
            defaults={
                "blocks_shift_input": blocks_shift_input,
                "display_order": display_order,
            },
        )


def _opening_schedule_for_date(group, work_date):
    return (
        DateOpeningSchedule.objects.filter(
            group=group,
            work_date=work_date,
            schedule_type__is_active=True,
        )
        .select_related("schedule_type")
        .first()
    )


def _opening_schedule_map(group, dates):
    dates = list(dates)
    date_schedules = {
        schedule.work_date: schedule
        for schedule in DateOpeningSchedule.objects.filter(
            group=group,
            work_date__in=dates,
            schedule_type__is_active=True,
        ).select_related("schedule_type")
    }
    return {work_date: date_schedules.get(work_date) for work_date in dates}


def _shift_input_is_blocked(group, work_date):
    opening_schedule = _opening_schedule_for_date(group, work_date)
    if opening_schedule is None:
        return False
    return opening_schedule.schedule_type.blocks_shift_input


def _open_shift_period(group):
    """現在シフト希望を入力できる対象期間（提出期限が来ていないもののうち最も近いもの）を返す"""
    return (
        ShiftDeadline.objects.filter(group=group, deadline_date__gte=_today())
        .order_by("deadline_date")
        .first()
    )


class DefaultShiftPeriod:
    """提出期限が未設定でも今月・来月分のシフト希望を入力できるようにする既定期間"""

    deadline_date = None
    note = ""
    is_default = True

    def __init__(self, period_start, period_end):
        self.period_start = period_start
        self.period_end = period_end


def _shift_entry_period(group):
    """シフト希望の入力対象期間。未設定のグループは今月初日〜来月末日を既定とする"""
    period = _open_shift_period(group)
    if period is not None:
        return period
    today = _today()
    _, next_month_end = _next_month_range(today)
    return DefaultShiftPeriod(date_type(today.year, today.month, 1), next_month_end)


def _next_month_range(today):
    year = today.year + 1 if today.month == 12 else today.year
    month = 1 if today.month == 12 else today.month + 1
    _, days_in_month = cal_module.monthrange(year, month)
    return date_type(year, month, 1), date_type(year, month, days_in_month)


def _month_from_value(month_value, today):
    """?month=YYYY-MM を月初日に変換する（不正な値は今月）"""
    try:
        year, month = (int(part) for part in month_value.split("-"))
        return date_type(year, month, 1)
    except (ValueError, TypeError):
        return date_type(today.year, today.month, 1)


def _add_month(month_first, delta):
    month_index = month_first.year * 12 + month_first.month - 1 + delta
    return date_type(month_index // 12, month_index % 12 + 1, 1)


def _month_bounds(month_first):
    _, days_in_month = cal_module.monthrange(month_first.year, month_first.month)
    return month_first, date_type(month_first.year, month_first.month, days_in_month)


def _month_weeks(month_first, today, cell_builder):
    """日曜始まりの月カレンダーを組み立てる。各日の中身は cell_builder(date) が返す"""
    calendar = cal_module.Calendar(firstweekday=6)
    weeks = []
    for raw_week in calendar.monthdayscalendar(month_first.year, month_first.month):
        days = []
        for day_num in raw_week:
            if day_num == 0:
                days.append(None)
                continue
            work_date = date_type(month_first.year, month_first.month, day_num)
            cell = {
                "date": work_date,
                "day_num": day_num,
                "is_today": work_date == today,
                "is_past": work_date < today,
                "is_saturday": work_date.weekday() == 5,
                "is_sunday": work_date.weekday() == 6,
            }
            cell.update(cell_builder(work_date))
            days.append(cell)
        weeks.append(days)
    return weeks


def _month_nav_context(current_month, today):
    return {
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "today_month_value": today.strftime("%Y-%m"),
        "is_current_month": current_month == date_type(today.year, today.month, 1),
    }


def _weekday_options(dates):
    """ダイアログの曜日一括用に、対象日を曜日ごとにまとめる"""
    weekday_dates = {}
    for work_date in dates:
        weekday_dates.setdefault(work_date.weekday(), []).append(work_date.day)
    return [
        {
            "value": weekday,
            "label": _WEEKDAY_LABELS[weekday],
            "days_label": ",".join(str(day) for day in weekday_dates[weekday]),
            "count": len(weekday_dates[weekday]),
        }
        for weekday in sorted(weekday_dates)
    ]


def _save_opening_schedules(group, dates, schedule_type_id, note):
    """指定日の開講区分を保存する。区分が未指定なら、その日の設定を削除する"""
    if not dates:
        return 0

    if schedule_type_id:
        schedule_type = OpeningScheduleType.objects.filter(group=group, id=schedule_type_id).first()
        if schedule_type is None:
            return 0
        with transaction.atomic():
            for work_date in dates:
                DateOpeningSchedule.objects.update_or_create(
                    group=group,
                    work_date=work_date,
                    defaults={"schedule_type": schedule_type, "note": note},
                )
        return len(dates)

    DateOpeningSchedule.objects.filter(group=group, work_date__in=dates).delete()
    return len(dates)


@login_required
def index(request):
    """所属グループごとのシフト入力導線"""
    today = _today()

    memberships = list(_user_memberships(request.user))
    admin_group_ids = {membership.group_id for membership in memberships if membership.is_admin}

    if request.user.is_staff:
        groups = list(_active_groups())
        admin_group_ids = {group.id for group in groups}
    else:
        groups = [membership.group for membership in memberships]

    draft_counts = {}
    for group in groups:
        draft_counts[group.id] = ShiftEntry.objects.filter(
            group=group, user=request.user, work_date__gte=today, is_draft=True,
        ).count()

    attendance_group_ids = set(
        AttendanceSetting.objects.filter(group__in=groups, is_enabled=True)
        .values_list("group_id", flat=True)
    )

    group_cards = [
        {
            "group": group,
            "is_admin": group.id in admin_group_ids,
            "draft_count": draft_counts.get(group.id, 0),
            "deadline": _open_shift_period(group),
            "attendance_enabled": group.id in attendance_group_ids,
        }
        for group in groups
    ]

    context = {
        "group_cards": group_cards,
        "today": today,
        "total_draft_count": sum(draft_counts.values()),
    }
    return render(request, "shift/index.html", context)


_SHIFT_STATUS_COLORS = {
    "available": "#198754",
    "maybe": "#fd7e14",
    "unavailable": "#6c757d",
}

_SHIFT_STATUS_SYMBOLS = {
    "available": "○",
    "maybe": "△",
    "unavailable": "×",
}


@login_required
def shift_calendar(request, group_id):
    """自分のシフト希望を月カレンダーで表示する"""
    group = _get_group_for_user(request, group_id)
    today = _today()
    current_month = _month_from_value(request.GET.get("month", ""), today)
    month_from, month_to = _month_bounds(current_month)
    month_dates = list(_date_range(month_from, month_to))

    time_slots = _active_time_slots()
    schedule_map = _opening_schedule_map(group, month_dates)
    entries_by_date = {}
    for entry in (
        ShiftEntry.objects.filter(
            group=group, user=request.user,
            work_date__gte=month_from, work_date__lte=month_to,
        )
        .select_related("time_slot")
        .order_by("time_slot__start_time")
    ):
        entries_by_date.setdefault(entry.work_date, []).append(entry)

    status_counts = {status: 0 for status, _label in ShiftEntry.STATUS_CHOICES}

    def build_cell(work_date):
        schedule = schedule_map.get(work_date)
        chips = []
        for entry in entries_by_date.get(work_date, []):
            status_counts[entry.status] = status_counts.get(entry.status, 0) + 1
            chips.append({
                "symbol": _SHIFT_STATUS_SYMBOLS.get(entry.status, "-"),
                "label": entry.time_slot.start_time.strftime("%H:%M"),
                "color": _SHIFT_STATUS_COLORS.get(entry.status, "#6c757d"),
                "title": "%s %s%s" % (
                    entry.time_slot.display_label,
                    entry.get_status_display(),
                    " / %s" % entry.note if entry.note else "",
                ),
                "is_draft": entry.is_draft,
            })
        return {
            "schedule": schedule,
            "schedule_color": _schedule_type_color(schedule.schedule_type) if schedule else "",
            "is_blocked": schedule is not None and schedule.schedule_type.blocks_shift_input,
            "chips": chips,
            "entry_url": "%s?date=%s" % (
                reverse("shift:entry_table", args=[group.id]), work_date.isoformat(),
            ),
        }

    weeks = _month_weeks(current_month, today, build_cell)
    draft_count = sum(
        1 for entry_list in entries_by_date.values() for entry in entry_list if entry.is_draft
    )

    summary = [
        {
            "status": status,
            "label": label,
            "symbol": symbol,
            "color": _SHIFT_STATUS_COLORS.get(status, "#6c757d"),
            "count": status_counts.get(status, 0),
        }
        for status, symbol, label in ENTRY_STATUS_CHOICES if status
    ]

    context = {
        "group": group,
        "weeks": weeks,
        "summary": summary,
        "draft_count": draft_count,
        "time_slots": time_slots,
        "entry_table_url": reverse("shift:entry_table", args=[group.id]),
        "has_entries": bool(entries_by_date),
    }
    context.update(_month_nav_context(current_month, today))
    return render(request, "shift/shift_calendar.html", context)


def _render_schedule_calendar(request, group, is_admin):
    """開講スケジュールの月カレンダー（管理者・メンバー共通）"""
    today = _today()
    current_month = _month_from_value(request.GET.get("month", ""), today)
    month_from, month_to = _month_bounds(current_month)
    month_dates = list(_date_range(month_from, month_to))
    schedule_map = _opening_schedule_map(group, month_dates)

    def build_cell(work_date):
        schedule = schedule_map.get(work_date)
        return {
            "schedule": schedule,
            "schedule_color": _schedule_type_color(schedule.schedule_type) if schedule else "",
            "is_blocked": schedule is not None and schedule.schedule_type.blocks_shift_input,
        }

    context = {
        "group": group,
        "weeks": _month_weeks(current_month, today, build_cell),
        "legend": _schedule_legend(group),
        "is_admin": is_admin,
        "settings_url": reverse("shift:schedule_settings", args=[group.id]) if is_admin else None,
    }
    context.update(_month_nav_context(current_month, today))
    return render(request, "shift/schedule.html", context)


@login_required
def schedule(request, group_id):
    """管理者向けの開講スケジュールカレンダー"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    _ensure_default_opening_schedule_types(group)
    return _render_schedule_calendar(request, group, is_admin=True)


@login_required
def schedule_member(request, group_id):
    """一般メンバー向けの開講スケジュールカレンダー"""
    group = _get_group_for_user(request, group_id)  # 管理者でなくても閲覧可
    return _render_schedule_calendar(request, group, is_admin=False)


# 表内はスペースが限られるため、記号ラベル + フルラベル(title属性用)の組で持たせる
ENTRY_STATUS_CHOICES = [
    (ShiftEntry.AVAILABLE, "○", "勤務可能"),
    (ShiftEntry.MAYBE, "△", "要相談"),
    (ShiftEntry.UNAVAILABLE, "×", "勤務不可"),
    ("", "－", "未回答"),
]

_WEEKDAY_LABELS = ("月曜", "火曜", "水曜", "木曜", "金曜", "土曜", "日曜")


def _period_months(period):
    """対象期間に含まれる月の初日リスト"""
    months = []
    year, month = period.period_start.year, period.period_start.month
    while (year, month) <= (period.period_end.year, period.period_end.month):
        months.append(date_type(year, month, 1))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _selected_month(month_value, months, today):
    """?month=YYYY-MM を期間内の月に丸める（既定は今日の月、期間外なら先頭の月）"""
    try:
        year, month = (int(part) for part in month_value.split("-"))
        selected = date_type(year, month, 1)
    except (ValueError, TypeError):
        selected = date_type(today.year, today.month, 1)
    if selected not in months:
        selected = months[0]
    return selected


def _month_range_in_period(month_first, period):
    _, days_in_month = cal_module.monthrange(month_first.year, month_first.month)
    month_last = date_type(month_first.year, month_first.month, days_in_month)
    return max(month_first, period.period_start), min(month_last, period.period_end)


def _save_entry_table(request, group, open_period, time_slots):
    """一覧画面のダイアログから送信されたシフト希望を保存する（日付単位 / 曜日一括）"""
    month_value = request.POST.get("month", "")
    redirect_url = "%s?month=%s" % (reverse("shift:entry_table", args=[group.id]), month_value)

    work_date = parse_date(request.POST.get("work_date", "") or "")
    if work_date:
        target_dates = [work_date]
    else:
        months = _period_months(open_period)
        month_first = _selected_month(month_value, months, _today())
        date_from, date_to = _month_range_in_period(month_first, open_period)
        try:
            weekday = int(request.POST.get("weekday", ""))
        except ValueError:
            weekday = None
        target_dates = [
            d for d in _date_range(date_from, date_to) if weekday is not None and d.weekday() == weekday
        ]

    target_dates = [
        d for d in target_dates if open_period.period_start <= d <= open_period.period_end
    ]
    if not target_dates:
        messages.error(request, "対象になる日付がありません。")
        return redirect(redirect_url)

    valid_statuses = {value for value, _label in ShiftEntry.STATUS_CHOICES}
    statuses = {
        time_slot.id: request.POST.get("status_%d" % time_slot.id, "")
        for time_slot in time_slots
    }
    comment = request.POST.get("comment", "").strip()

    saved_days = 0
    blocked_days = 0
    with transaction.atomic():
        for target_date in target_dates:
            if _shift_input_is_blocked(group, target_date):
                blocked_days += 1
                continue
            for time_slot in time_slots:
                status = statuses.get(time_slot.id, "")
                if status in valid_statuses:
                    ShiftEntry.objects.update_or_create(
                        group=group,
                        user=request.user,
                        work_date=target_date,
                        time_slot=time_slot,
                        defaults={"status": status, "note": comment, "is_draft": True},
                    )
                else:
                    ShiftEntry.objects.filter(
                        group=group,
                        user=request.user,
                        work_date=target_date,
                        time_slot=time_slot,
                    ).delete()
            saved_days += 1

    message = "%d日分のシフト希望を登録しました。" % saved_days
    if blocked_days:
        message += "（休校等の%d日はスキップしました）" % blocked_days
    messages.success(request, message)
    return redirect(redirect_url)


@login_required
def entry_table(request, group_id):
    """月ごとの一覧表。日付をタップしてその日（または曜日一括）の希望をまとめて登録する"""
    group = _get_group_for_user(request, group_id)
    open_period = _shift_entry_period(group)
    time_slots = _active_time_slots()

    if request.method == "POST":
        return _save_entry_table(request, group, open_period, time_slots)

    today = _today()
    months = _period_months(open_period)
    # ?date= が指定された場合はその日を含む月を開き、読み込み後にその日のダイアログを表示する
    focus_date = parse_date(request.GET.get("date", "") or "")
    if focus_date and not (open_period.period_start <= focus_date <= open_period.period_end):
        focus_date = None
    month_value = focus_date.strftime("%Y-%m") if focus_date else request.GET.get("month", "")
    current_month = _selected_month(month_value, months, today)
    month_index = months.index(current_month)
    date_from, date_to = _month_range_in_period(current_month, open_period)
    dates = list(_date_range(date_from, date_to))
    opening_schedule_by_date = _opening_schedule_map(group, dates)

    existing_entries = {
        (entry.work_date, entry.time_slot_id): entry
        for entry in ShiftEntry.objects.filter(
            group=group,
            user=request.user,
            work_date__gte=date_from,
            work_date__lte=date_to,
        ).select_related("time_slot")
    }

    rows = []
    open_dates = []
    draft_count = 0
    for work_date in dates:
        opening_schedule = opening_schedule_by_date.get(work_date)
        is_shift_blocked = opening_schedule is not None and opening_schedule.schedule_type.blocks_shift_input
        cells = []
        statuses = {}
        comment = ""
        for time_slot in time_slots:
            entry = existing_entries.get((work_date, time_slot.id))
            cells.append({
                "time_slot": time_slot,
                "entry": entry,
                "status": entry.status if entry else "",
            })
            statuses[str(time_slot.id)] = entry.status if entry else ""
            if entry:
                if entry.is_draft:
                    draft_count += 1
                if entry.note and not comment:
                    comment = entry.note
        if not is_shift_blocked:
            open_dates.append(work_date)
        rows.append({
            "work_date": work_date,
            "opening_schedule": opening_schedule,
            "is_shift_blocked": is_shift_blocked,
            "is_today": work_date == today,
            "cells": cells,
            "statuses_json": json.dumps(statuses),
            "comment": comment,
        })

    return render(request, "shift/entry_table.html", {
        "group": group,
        "time_slots": time_slots,
        "rows": rows,
        "status_choices": ENTRY_STATUS_CHOICES,
        "date_from": date_from,
        "date_to": date_to,
        "open_period": open_period,
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": months[month_index - 1] if month_index > 0 else None,
        "next_month": months[month_index + 1] if month_index + 1 < len(months) else None,
        "weekday_options": _weekday_options(open_dates),
        "draft_count": draft_count,
        "focus_date": focus_date,
    })


@login_required
def shift_confirm(request, group_id):
    """ログイン中のユーザ自身の下書きシフト希望を一括確定する（他メンバーのぶんは対象外）"""
    group = _get_group_for_user(request, group_id)
    today = _today()

    if request.method == "POST":
        date_from = _parse_date_or_default(request.POST.get("date_from", ""), today)
        date_to = _parse_date_or_default(request.POST.get("date_to", ""), today)
        count = ShiftEntry.objects.filter(
            group=group,
            user=request.user,
            work_date__gte=date_from,
            work_date__lte=date_to,
            is_draft=True,
        ).update(is_draft=False)
        messages.success(request, "%d件のシフト希望を確定しました。" % count)
        return redirect("%s?group=%d" % (reverse("shift:index"), group.id))

    # GET: 確定対象の下書きを表示
    draft_entries = (
        ShiftEntry.objects.filter(
            group=group,
            user=request.user,
            work_date__gte=today,
            is_draft=True,
        )
        .select_related("time_slot")
        .order_by("work_date", "time_slot__start_time")
    )
    if not draft_entries.exists():
        messages.info(request, "確定する下書きのシフト希望がありません。")
        return redirect("%s?group=%d" % (reverse("shift:index"), group.id))

    # 期間
    dates = sorted({e.work_date for e in draft_entries})
    date_from = dates[0]
    date_to = dates[-1]

    opening_schedule_by_date = _opening_schedule_map(group, dates)

    entries_by_date = {}
    for entry in draft_entries:
        entries_by_date.setdefault(entry.work_date, []).append(entry)

    summary_rows = [
        {
            "work_date": d,
            "opening_schedule": opening_schedule_by_date.get(d),
            "entries": entries_by_date.get(d, []),
        }
        for d in dates
    ]

    return render(request, "shift/shift_confirm.html", {
        "group": group,
        "summary_rows": summary_rows,
        "date_from": date_from,
        "date_to": date_to,
        "draft_count": len(draft_entries),
    })


def _admin_group_landing(request, title, target_name):
    groups = _admin_groups_for_user(request.user)
    if not groups:
        raise PermissionDenied
    if len(groups) == 1:
        return redirect(target_name, group_id=groups[0].id)

    group_cards = [
        {
            "group": group,
            "url": reverse(target_name, args=[group.id]),
        }
        for group in groups
    ]
    return render(request, "shift/admin_group_select.html", {
        "title": title,
        "group_cards": group_cards,
    })


@login_required
def schedule_index(request):
    """開講スケジュール管理のグループ選択"""
    return _admin_group_landing(request, "開講スケジュール", "shift:schedule")


@login_required
def summary_index(request):
    """提出状況のグループ選択"""
    return _admin_group_landing(request, "提出状況", "shift:summary")


_SCHEDULE_TYPE_PALETTE = [
    "#0d6efd",  # blue
    "#198754",  # green
    "#fd7e14",  # orange
    "#6f42c1",  # purple
    "#20c997",  # teal
    "#d63384",  # pink
    "#ffc107",  # yellow
    "#0dcaf0",  # cyan
]


def _schedule_type_color(schedule_type):
    """開講区分に対応する色を返す（blocks_shift_input のもの は赤固定）"""
    if schedule_type.blocks_shift_input:
        return "#dc3545"
    # id をパレット数で割り切った位置の色を返す
    return _SCHEDULE_TYPE_PALETTE[schedule_type.id % len(_SCHEDULE_TYPE_PALETTE)]


def _schedule_legend(group):
    """グループの開講区分の凡例データを返す"""
    schedule_types = list(
        OpeningScheduleType.objects.filter(group=group, is_active=True).order_by("display_order", "name")
    )
    return [{"name": st.name, "color": _schedule_type_color(st)} for st in schedule_types]


@login_required
def schedule_settings(request, group_id):
    """管理者向けの開講スケジュール設定"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    _ensure_default_opening_schedule_types(group)

    today = _today()
    action = request.POST.get("action", "")
    month_value = request.POST.get("month", "") if request.method == "POST" else request.GET.get("month", "")
    current_month = _month_from_value(month_value, today)
    month_from, month_to = _month_bounds(current_month)
    month_dates = list(_date_range(month_from, month_to))
    next_month_start, next_month_end = _next_month_range(today)

    if request.method == "POST" and action == "schedule_type":
        schedule_type = OpeningScheduleType.objects.filter(
            group=group, id=request.POST.get("schedule_type_id", "") or 0,
        ).first()
        type_form = OpeningScheduleTypeForm(request.POST, instance=schedule_type, group=group)
        if type_form.is_valid():
            saved_type = type_form.save(commit=False)
            saved_type.group = group
            saved_type.save()
            messages.success(request, "開講区分「%s」を保存しました。" % saved_type.name)
        else:
            messages.error(request, "開講区分を保存できませんでした。%s" % type_form.errors.as_text())
        return redirect("shift:schedule_settings", group_id=group.id)

    if request.method == "POST" and action in {"schedule_date", "schedule_weekday"}:
        redirect_url = "%s?month=%s" % (
            reverse("shift:schedule_settings", args=[group.id]),
            current_month.strftime("%Y-%m"),
        )
        if action == "schedule_date":
            work_date = parse_date(request.POST.get("work_date", "") or "")
            target_dates = [work_date] if work_date else []
        else:
            try:
                weekday = int(request.POST.get("weekday", ""))
            except ValueError:
                weekday = None
            target_dates = [d for d in month_dates if weekday is not None and d.weekday() == weekday]

        if not target_dates:
            messages.error(request, "対象になる日付がありません。")
            return redirect(redirect_url)

        schedule_type_id = request.POST.get("schedule_type", "")
        saved = _save_opening_schedules(
            group, target_dates, schedule_type_id, request.POST.get("note", "").strip(),
        )
        if not saved:
            messages.error(request, "開講区分の保存に失敗しました。")
        elif schedule_type_id:
            messages.success(request, "%d日分の開講スケジュールを保存しました。" % saved)
        else:
            messages.success(request, "%d日分を未設定（曜日の基本スケジュール）に戻しました。" % saved)
        return redirect(redirect_url)

    if request.method == "POST" and action == "deadline":
        deadline = ShiftDeadline.objects.filter(
            group=group, id=request.POST.get("deadline_id", "") or 0,
        ).first()
        deadline_form = ShiftDeadlineForm(request.POST, instance=deadline)
        if deadline_form.is_valid():
            cleaned = deadline_form.cleaned_data
            # (group, period_start) は一意なので、同じ開始日の期限があればそれを更新する
            ShiftDeadline.objects.update_or_create(
                group=group,
                period_start=cleaned["period_start"],
                defaults={
                    "period_end": cleaned["period_end"],
                    "deadline_date": cleaned["deadline_date"],
                    "note": cleaned["note"],
                },
            )
            if deadline is not None and deadline.period_start != cleaned["period_start"]:
                deadline.delete()
            messages.success(request, "提出期限を保存しました。")
        else:
            messages.error(request, "提出期限を保存できませんでした。%s" % deadline_form.errors.as_text())
        return redirect("shift:schedule_settings", group_id=group.id)

    schedule_types = OpeningScheduleType.objects.filter(group=group).order_by("display_order", "name")
    schedule_type_rows = [
        {
            "schedule_type": schedule_type,
            "color": _schedule_type_color(schedule_type),
        }
        for schedule_type in schedule_types
    ]
    deadlines = ShiftDeadline.objects.filter(group=group).order_by("-period_start")[:24]
    open_period = _open_shift_period(group)

    # 日付別スケジュール（月表示・ダイアログ編集用）
    schedule_by_date = {
        schedule.work_date: schedule
        for schedule in DateOpeningSchedule.objects.filter(
            group=group, work_date__gte=month_from, work_date__lte=month_to,
        ).select_related("schedule_type")
    }
    schedule_rows = []
    for work_date in month_dates:
        date_schedule = schedule_by_date.get(work_date)
        schedule_rows.append({
            "work_date": work_date,
            "date_schedule": date_schedule,
            "color": _schedule_type_color(date_schedule.schedule_type) if date_schedule else "",
            "is_today": work_date == today,
            "schedule_type_id": date_schedule.schedule_type_id if date_schedule else "",
            "note": date_schedule.note if date_schedule else "",
        })

    schedule_type_options = [
        {
            "id": schedule_type.id,
            "name": schedule_type.name,
            "color": _schedule_type_color(schedule_type),
            "blocks_shift_input": schedule_type.blocks_shift_input,
        }
        for schedule_type in schedule_types
        if schedule_type.is_active
    ]

    return render(request, "shift/schedule_settings.html", {
        "group": group,
        "schedule_types": schedule_types,
        "schedule_type_rows": schedule_type_rows,
        "schedule_type_options": schedule_type_options,
        "schedule_rows": schedule_rows,
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "weekday_options": _weekday_options(month_dates),
        "default_deadline_start": next_month_start,
        "default_deadline_end": next_month_end,
        "default_deadline_date": today,
        "deadlines": deadlines,
        "open_period": open_period,
        "calendar_url": reverse("shift:schedule", args=[group.id]),
    })


@login_required
def deadline_delete(request, group_id, deadline_id):
    """シフト提出期限を削除"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    deadline = get_object_or_404(ShiftDeadline, id=deadline_id, group=group)

    if request.method != "POST":
        raise PermissionDenied

    deadline.delete()
    messages.success(request, "提出期限を削除しました。")
    return redirect("shift:schedule_settings", group_id=group.id)


@login_required
def summary(request, group_id):
    """グループ管理者向けのシフト希望サマリ（提出状況）"""
    group = _get_group_for_user(request, group_id, require_admin=True)

    # デフォルトは当月1日から月末まで
    today = _today()
    this_month_from, this_month_to = _month_bounds(date_type(today.year, today.month, 1))
    next_month_from, next_month_to = _next_month_range(today)

    date_from = _parse_date_or_default(request.GET.get("from", ""), this_month_from)
    try:
        days = int(request.GET.get("days", ""))
    except ValueError:
        days = 0
    if days <= 0:
        # 日数未指定なら開始日の属する月の末日までを既定にする
        _, month_end = _month_bounds(date_type(date_from.year, date_from.month, 1))
        days = (month_end - date_from).days + 1
    days = min(max(days, 1), 31)
    date_to = date_from + timedelta(days=days - 1)

    members = sorted(
        (
            membership.user
            for membership in (
                UserGroup.objects.filter(group=group, user__is_active=True)
                .select_related("user")
            )
        ),
        key=lambda user: (user.get_display_name(), user.username),
    )
    time_slots = list(TimeSlot.objects.filter(is_active=True).order_by("start_time"))

    # 下書きは「未提出」なので提出状況には含めない
    entries = ShiftEntry.objects.filter(
        group=group,
        work_date__gte=date_from,
        work_date__lte=date_to,
        is_draft=False,
    ).select_related("user", "time_slot")
    # entry_map[(user_id, work_date, time_slot_id)] = entry
    entry_map = {
        (e.user_id, e.work_date, e.time_slot_id): e
        for e in entries
    }

    dates = [date_from + timedelta(days=offset) for offset in range(days)]
    opening_schedule_by_date = _opening_schedule_map(group, dates)

    # 日付ヘッダー情報
    date_headers = []
    previous_month = None
    for work_date in dates:
        sched = opening_schedule_by_date.get(work_date)
        date_headers.append({
            "work_date": work_date,
            "weekday": work_date.weekday(),  # 0=月 … 5=土 6=日
            "is_saturday": work_date.weekday() == 5,
            "is_sunday": work_date.weekday() == 6,
            "is_today": work_date == today,
            "show_month": work_date.month != previous_month,
            "opening_schedule": sched,
            "schedule_color": _schedule_type_color(sched.schedule_type) if sched else "",
            "is_blocked": sched is not None and sched.schedule_type.blocks_shift_input,
        })
        previous_month = work_date.month

    # 入力対象日（休校などシフト入力できない日を除く）
    target_days = sum(0 if dh["is_blocked"] else 1 for dh in date_headers)

    # 日付×時間帯ごとの「勤務可能」人数
    day_slot_counts = {
        (dh["work_date"], ts.id): {"available": 0, "maybe": 0}
        for dh in date_headers
        for ts in time_slots
    }

    # メンバー行
    member_rows = []
    for member in members:
        date_cells = []
        available_days = 0
        answered_days = 0
        for dh in date_headers:
            work_date = dh["work_date"]
            slot_cells = []
            has_any = False
            has_available = False
            for ts in time_slots:
                entry = entry_map.get((member.id, work_date, ts.id))
                slot_cells.append({"time_slot": ts, "entry": entry})
                if entry:
                    has_any = True
                    if entry.status == ShiftEntry.AVAILABLE:
                        has_available = True
                    if not dh["is_blocked"] and entry.status in (ShiftEntry.AVAILABLE, ShiftEntry.MAYBE):
                        counts = day_slot_counts[(work_date, ts.id)]
                        counts["available" if entry.status == ShiftEntry.AVAILABLE else "maybe"] += 1
            if not dh["is_blocked"]:
                if has_available:
                    available_days += 1
                if has_any:
                    answered_days += 1
            date_cells.append({
                "work_date": work_date,
                "is_saturday": dh["is_saturday"],
                "is_sunday": dh["is_sunday"],
                "is_blocked": dh["is_blocked"],
                "is_today": dh["is_today"],
                "slot_cells": slot_cells,
                "has_any": has_any,
            })

        if not target_days:
            status = "na"
            status_label = "対象日なし"
        elif answered_days >= target_days:
            status = "done"
            status_label = "提出済"
        elif answered_days:
            status = "partial"
            status_label = "一部"
        else:
            status = "none"
            status_label = "未提出"

        member_rows.append({
            "member": member,
            "date_cells": date_cells,
            "available_days": available_days,
            "answered_days": answered_days,
            "missing_days": max(target_days - answered_days, 0),
            "status": status,
            "status_label": status_label,
        })

    # 並び順（既定は未提出の人を上に出して、声をかける相手が分かるようにする）
    sort = "name" if request.GET.get("sort", "") == "name" else "status"
    if sort == "status":
        status_order = {"none": 0, "partial": 1, "done": 2, "na": 3}
        member_rows.sort(
            key=lambda row: (
                status_order.get(row["status"], 9),
                row["member"].get_display_name(),
                row["member"].username,
            )
        )

    # 日別の合計行
    day_totals = []
    for dh in date_headers:
        day_totals.append({
            "work_date": dh["work_date"],
            "is_saturday": dh["is_saturday"],
            "is_sunday": dh["is_sunday"],
            "is_blocked": dh["is_blocked"],
            "is_today": dh["is_today"],
            "slot_counts": [
                {
                    "time_slot": ts,
                    "available": day_slot_counts[(dh["work_date"], ts.id)]["available"],
                    "maybe": day_slot_counts[(dh["work_date"], ts.id)]["maybe"],
                }
                for ts in time_slots
            ],
        })

    slot_legend = []
    for index, ts in enumerate(time_slots, start=1):
        time_range = "%s〜%s" % (ts.start_time.strftime("%H:%M"), ts.end_time.strftime("%H:%M"))
        normalized_name = ts.name.replace("-", "〜").replace("–", "〜").replace("~", "〜").strip()
        slot_legend.append({
            "index": index,
            "time_slot": ts,
            "time_range": time_range,
            "name": "" if normalized_name == time_range else ts.name,
        })

    stats = {
        "members": len(member_rows),
        "done": sum(1 for row in member_rows if row["status"] == "done"),
        "partial": sum(1 for row in member_rows if row["status"] == "partial"),
        "none": sum(1 for row in member_rows if row["status"] == "none"),
        "target_days": target_days,
        "blocked_days": days - target_days,
    }

    # 期間が暦月ぴったりのときは、前後も暦月単位で移動する（月末が欠けないように）
    month_first = date_type(date_from.year, date_from.month, 1)
    _, month_last = _month_bounds(month_first)
    is_whole_month = date_from == month_first and date_to == month_last
    if is_whole_month:
        previous_from, previous_to = _month_bounds(_add_month(month_first, -1))
        next_from, next_to = _month_bounds(_add_month(month_first, 1))
        previous_days = (previous_to - previous_from).days + 1
        next_days = (next_to - next_from).days + 1
    else:
        previous_from = date_from - timedelta(days=days)
        next_from = date_from + timedelta(days=days)
        previous_days = next_days = days

    # 対象期間に重なる提出期限
    deadline = (
        ShiftDeadline.objects.filter(
            group=group, period_start__lte=date_to, period_end__gte=date_from,
        )
        .order_by("deadline_date")
        .first()
    )

    context = {
        "group": group,
        "members": members,
        "time_slots": time_slots,
        "slot_legend": slot_legend,
        "date_headers": date_headers,
        "member_rows": member_rows,
        "day_totals": day_totals,
        "stats": stats,
        "deadline": deadline,
        "deadline_passed": deadline is not None and deadline.deadline_date < today,
        "schedule_legend": _schedule_legend(group),
        "today": today,
        "date_from": date_from,
        "date_to": date_to,
        "days": days,
        "day_options": sorted({7, 14, 31, days}),
        "sort": sort,
        "previous_from": previous_from,
        "previous_days": previous_days,
        "next_from": next_from,
        "next_days": next_days,
        "is_whole_month": is_whole_month,
        "this_month_from": this_month_from,
        "this_month_days": (this_month_to - this_month_from).days + 1,
        "next_month_from": next_month_from,
        "next_month_days": (next_month_to - next_month_from).days + 1,
    }
    return render(request, "shift/summary.html", context)
