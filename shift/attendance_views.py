"""勤怠管理（出退勤の記録）のビュー

グループごとに機能の有効・無効を切り替えられ、入力方法は次の2通り。

* 手動入力: 月ごとの一覧表（Excel の表のように）から1日ぶんをまとめて入力する
* 打刻: 出勤ボタン・退勤ボタンで現在時刻を記録する（管理者が無効化できる）
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from custom_auth.models import UserGroup
from shift.forms import AttendanceRecordForm, AttendanceSettingForm
from shift.models import AttendanceRecord, AttendanceSetting, minutes_to_hhmm
from shift.views import (
    _active_groups,
    _add_month,
    _admin_group_landing,
    _date_range,
    _get_group_for_user,
    _month_bounds,
    _month_from_value,
    _opening_schedule_map,
    _today,
    _user_memberships,
)


def _date_label(work_date):
    return "%d月%d日" % (work_date.month, work_date.day)


def _attendance_setting(group):
    """グループの勤怠設定を取得する（未作成なら既定値で作成する）"""
    setting, _created = AttendanceSetting.objects.get_or_create(group=group)
    return setting


def _enabled_group_ids(groups):
    """勤怠管理が有効になっているグループの id 集合"""
    return set(
        AttendanceSetting.objects.filter(group__in=groups, is_enabled=True)
        .values_list("group_id", flat=True)
    )


def _month_records(group, user, month_from, month_to):
    """指定月の勤怠実績を日付をキーにして返す"""
    return {
        record.work_date: record
        for record in AttendanceRecord.objects.filter(
            group=group, user=user, work_date__gte=month_from, work_date__lte=month_to,
        )
    }


def _month_totals(records):
    """勤務日数と実働時間の合計"""
    worked_days = 0
    total_minutes = 0
    for record in records:
        minutes = record.worked_minutes
        if record.start_time:
            worked_days += 1
        if minutes:
            total_minutes += minutes
    return {
        "worked_days": worked_days,
        "total_minutes": total_minutes,
        "total_display": minutes_to_hhmm(total_minutes),
    }


def _save_attendance_record(request, group, user, work_date, redirect_url):
    """1日ぶんの勤怠を保存する（出勤・退勤が空なら削除）"""
    record = AttendanceRecord.objects.filter(group=group, user=user, work_date=work_date).first()
    form = AttendanceRecordForm(request.POST, instance=record)
    if not form.is_valid():
        messages.error(request, "勤怠を保存できませんでした。%s" % form.errors.as_text())
        return redirect(redirect_url)

    cleaned = form.cleaned_data
    if not cleaned["start_time"] and not cleaned["end_time"]:
        if record is not None:
            record.delete()
            messages.success(request, "%s の勤怠を削除しました。" % _date_label(work_date))
        else:
            messages.info(request, "入力がなかったため、登録しませんでした。")
        return redirect(redirect_url)

    saved = form.save(commit=False)
    saved.group = group
    saved.user = user
    saved.work_date = work_date
    saved.source = AttendanceRecord.MANUAL
    saved.save()
    messages.success(request, "%s の勤怠を保存しました。" % _date_label(work_date))
    return redirect(redirect_url)


@login_required
def attendance_index(request):
    """勤怠管理が有効な所属グループの一覧"""
    if request.user.is_staff:
        groups = list(_active_groups())
    else:
        groups = [membership.group for membership in _user_memberships(request.user)]

    enabled_ids = _enabled_group_ids(groups)
    enabled_groups = [group for group in groups if group.id in enabled_ids]
    if len(enabled_groups) == 1:
        return redirect("shift:attendance", group_id=enabled_groups[0].id)

    today = _today()
    today_records = {
        record.group_id: record
        for record in AttendanceRecord.objects.filter(
            group__in=enabled_groups, user=request.user, work_date=today,
        )
    }
    group_cards = [
        {
            "group": group,
            "record": today_records.get(group.id),
            "url": reverse("shift:attendance", args=[group.id]),
        }
        for group in enabled_groups
    ]
    return render(request, "shift/attendance_index.html", {
        "group_cards": group_cards,
        "today": today,
        "has_disabled_groups": len(groups) > len(enabled_groups),
    })


@login_required
def attendance(request, group_id):
    """自分の勤怠を月ごとの一覧表で入力・確認する"""
    group = _get_group_for_user(request, group_id)
    setting = _attendance_setting(group)
    if not setting.is_enabled:
        messages.info(request, "このグループでは勤怠管理が有効になっていません。")
        return redirect("shift:index")

    today = _today()
    month_value = request.POST.get("month", "") if request.method == "POST" else request.GET.get("month", "")
    current_month = _month_from_value(month_value, today)
    month_from, month_to = _month_bounds(current_month)

    if request.method == "POST":
        redirect_url = "%s?month=%s" % (
            reverse("shift:attendance", args=[group.id]), current_month.strftime("%Y-%m"),
        )
        if not setting.allow_manual_input:
            messages.error(request, "このグループでは手動入力が許可されていません。")
            return redirect(redirect_url)
        work_date = parse_date(request.POST.get("work_date", "") or "")
        if work_date is None:
            messages.error(request, "対象の日付が不正です。")
            return redirect(redirect_url)
        return _save_attendance_record(request, group, request.user, work_date, redirect_url)

    records = _month_records(group, request.user, month_from, month_to)
    month_dates = list(_date_range(month_from, month_to))
    opening_schedule_by_date = _opening_schedule_map(group, month_dates)

    rows = [
        {
            "work_date": work_date,
            "record": records.get(work_date),
            "opening_schedule": opening_schedule_by_date.get(work_date),
            "is_today": work_date == today,
            "is_future": work_date > today,
            "is_saturday": work_date.weekday() == 5,
            "is_sunday": work_date.weekday() == 6,
        }
        for work_date in month_dates
    ]

    today_record = AttendanceRecord.objects.filter(
        group=group, user=request.user, work_date=today,
    ).first()
    open_record = _open_clock_record(group, request.user, today)

    context = {
        "group": group,
        "setting": setting,
        "rows": rows,
        "totals": _month_totals(records.values()),
        "today": today,
        "today_record": today_record,
        "open_record": open_record,
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "today_month_value": today.strftime("%Y-%m"),
        "is_current_month": current_month == today.replace(day=1),
    }
    return render(request, "shift/attendance.html", context)


def _open_clock_record(group, user, today):
    """退勤が未記録の勤怠を返す（当日になければ前日の夜勤ぶんを探す）"""
    record = AttendanceRecord.objects.filter(
        group=group, user=user, work_date=today, start_time__isnull=False, end_time__isnull=True,
    ).first()
    if record is not None:
        return record
    return AttendanceRecord.objects.filter(
        group=group, user=user, work_date=today - timedelta(days=1),
        start_time__isnull=False, end_time__isnull=True,
    ).first()


@login_required
def attendance_clock(request, group_id):
    """出勤ボタン・退勤ボタンによる打刻"""
    group = _get_group_for_user(request, group_id)
    setting = _attendance_setting(group)
    redirect_url = reverse("shift:attendance", args=[group.id])

    if request.method != "POST":
        raise PermissionDenied
    if not setting.is_enabled or not setting.allow_clock_button:
        messages.error(request, "このグループでは出退勤ボタンが利用できません。")
        return redirect(redirect_url)

    today = _today()
    now_time = timezone.now().time().replace(second=0, microsecond=0)
    action = request.POST.get("action", "")

    if action == "in":
        open_record = _open_clock_record(group, request.user, today)
        if open_record is not None and open_record.work_date != today:
            messages.warning(request, "%s の退勤が記録されていません。一覧表から修正してください。"
                             % _date_label(open_record.work_date))
        record, _created = AttendanceRecord.objects.get_or_create(
            group=group, user=request.user, work_date=today,
            defaults={"source": AttendanceRecord.CLOCK},
        )
        if record.start_time:
            messages.info(request, "本日はすでに出勤を記録しています。（%s）"
                          % record.start_time.strftime("%H:%M"))
            return redirect(redirect_url)
        record.start_time = now_time
        record.source = AttendanceRecord.CLOCK
        record.save()
        messages.success(request, "出勤を記録しました。（%s）" % now_time.strftime("%H:%M"))
        return redirect(redirect_url)

    if action == "out":
        record = _open_clock_record(group, request.user, today)
        if record is None:
            messages.error(request, "出勤が記録されていません。先に出勤ボタンを押してください。")
            return redirect(redirect_url)
        record.end_time = now_time
        record.source = AttendanceRecord.CLOCK
        record.save()
        messages.success(request, "退勤を記録しました。（%s／実働 %s）"
                         % (now_time.strftime("%H:%M"), record.worked_time_display))
        return redirect(redirect_url)

    messages.error(request, "操作が不正です。")
    return redirect(redirect_url)


@login_required
def attendance_admin_index(request):
    """勤怠管理のグループ選択（管理者）"""
    return _admin_group_landing(request, "勤怠管理", "shift:attendance_summary")


@login_required
def attendance_settings(request, group_id):
    """管理者向けの勤怠管理設定（機能の有効・無効、入力方法の許可）"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    setting = _attendance_setting(group)

    form = AttendanceSettingForm(request.POST or None, instance=setting)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "勤怠設定を保存しました。")
            return redirect("shift:attendance_settings", group_id=group.id)
        messages.error(request, "勤怠設定を保存できませんでした。")

    return render(request, "shift/attendance_settings.html", {
        "group": group,
        "setting": setting,
        "form": form,
        "summary_url": reverse("shift:attendance_summary", args=[group.id]),
    })


@login_required
def attendance_summary(request, group_id):
    """管理者向けの勤怠集計（メンバー×日付）。セルから勤怠の修正もできる"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    setting = _attendance_setting(group)

    today = _today()
    month_value = request.POST.get("month", "") if request.method == "POST" else request.GET.get("month", "")
    current_month = _month_from_value(month_value, today)
    month_from, month_to = _month_bounds(current_month)

    memberships = list(
        UserGroup.objects.filter(group=group, user__is_active=True)
        .select_related("user")
        .order_by("user__username")
    )
    members = [membership.user for membership in memberships]

    if request.method == "POST":
        redirect_url = "%s?month=%s" % (
            reverse("shift:attendance_summary", args=[group.id]), current_month.strftime("%Y-%m"),
        )
        if not setting.is_enabled:
            messages.error(request, "勤怠管理が有効になっていません。")
            return redirect(redirect_url)
        target = next(
            (member for member in members if str(member.id) == request.POST.get("user_id", "")), None,
        )
        work_date = parse_date(request.POST.get("work_date", "") or "")
        if target is None or work_date is None:
            messages.error(request, "対象のメンバーまたは日付が不正です。")
            return redirect(redirect_url)
        return _save_attendance_record(request, group, target, work_date, redirect_url)

    month_dates = list(_date_range(month_from, month_to))
    opening_schedule_by_date = _opening_schedule_map(group, month_dates)
    records = {
        (record.user_id, record.work_date): record
        for record in AttendanceRecord.objects.filter(
            group=group, work_date__gte=month_from, work_date__lte=month_to,
        )
    }

    date_headers = [
        {
            "work_date": work_date,
            "is_today": work_date == today,
            "is_saturday": work_date.weekday() == 5,
            "is_sunday": work_date.weekday() == 6,
            "opening_schedule": opening_schedule_by_date.get(work_date),
        }
        for work_date in month_dates
    ]

    member_rows = []
    for member in members:
        member_records = [
            records[(member.id, work_date)]
            for work_date in month_dates
            if (member.id, work_date) in records
        ]
        cells = []
        for header in date_headers:
            record = records.get((member.id, header["work_date"]))
            cells.append({
                "work_date": header["work_date"],
                "record": record,
                "is_saturday": header["is_saturday"],
                "is_sunday": header["is_sunday"],
                "is_today": header["is_today"],
            })
        member_rows.append({
            "member": member,
            "cells": cells,
            "totals": _month_totals(member_records),
        })

    context = {
        "group": group,
        "setting": setting,
        "date_headers": date_headers,
        "member_rows": member_rows,
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "today_month_value": today.strftime("%Y-%m"),
        "is_current_month": current_month == today.replace(day=1),
        "settings_url": reverse("shift:attendance_settings", args=[group.id]),
    }
    return render(request, "shift/attendance_summary.html", context)
