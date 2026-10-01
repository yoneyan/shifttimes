"""勤怠管理（出退勤の記録）のビュー

グループごとに機能の有効・無効を切り替えられ、入力方法は次の2通り。

* 手動入力: 月ごとの一覧表（Excel の表のように）から勤怠を1件ずつ入力する
* 打刻: 出勤ボタン・退勤ボタンで現在時刻を記録する（管理者が無効化できる）

勤怠は同じ日に何件でも登録できる（時間帯が重なるものは不可）。
管理者が仕事内容と時給を用意すると、勤怠ごとに仕事内容を選び、実働時間 × 時給で金額を出す。
"""
import csv
from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import content_disposition_header
from django.utils.text import get_valid_filename

from custom_auth.models import UserGroup
from shift.forms import AttendanceRecordForm, AttendanceSettingForm, WorkTypeForm
from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    WorkType,
    find_overlapping_record,
    format_yen,
    minutes_to_hhmm,
)
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


def _parse_work_date(value):
    """フォームから来た日付。形式が正しくても存在しない日付（2月30日など）は None"""
    try:
        return parse_date(value or "")
    except ValueError:
        return None


def _form_error_text(form):
    """フォームのエラーを1行の文にする（項目のエラーは「項目名: 内容」）"""
    texts = list(form.non_field_errors())
    for name, errors in form.errors.items():
        if name in form.fields:
            texts.extend(f"{form.fields[name].label}: {error}" for error in errors)
    return " ".join(texts)


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


def _work_type_context(group):
    """入力欄に出す仕事内容（無効なものも含む。登録済みの勤怠を編集するときに必要）"""
    work_types = list(WorkType.objects.filter(group=group))
    return {
        "work_types": work_types,
        "has_active_work_types": any(work_type.is_active for work_type in work_types),
    }


def _month_records(group, user, month_from, month_to):
    """指定月の勤怠実績（日付・出勤時刻順）"""
    return list(
        AttendanceRecord.objects.filter(
            group=group, user=user, work_date__gte=month_from, work_date__lte=month_to,
        ).select_related("work_type")
    )


def _records_by_date(records):
    by_date = defaultdict(list)
    for record in records:
        by_date[record.work_date].append(record)
    return by_date


def _work_type_sort_key(work_type):
    """仕事内容の並び順。未設定（None）は最後"""
    if work_type is None:
        return (1, 0, "")
    return (0, work_type.display_order, work_type.name)


def _month_totals(records):
    """勤務日数・実働時間・金額の合計と、仕事内容ごとの内訳"""
    worked_dates = set()
    total_minutes = 0
    total_amount = 0
    breakdown = {}
    for record in records:
        if record.start_time:
            worked_dates.add(record.work_date)
        minutes = record.worked_minutes
        if not minutes:
            continue
        total_minutes += minutes
        item = breakdown.setdefault(record.work_type_id, {
            "work_type": record.work_type,
            "minutes": 0,
            "amount": None,
        })
        item["minutes"] += minutes
        amount = record.amount
        if amount is not None:
            total_amount += amount
            item["amount"] = (item["amount"] or 0) + amount

    by_work_type = sorted(breakdown.values(), key=lambda item: _work_type_sort_key(item["work_type"]))
    for item in by_work_type:
        item["time_display"] = minutes_to_hhmm(item["minutes"])
        item["amount_display"] = format_yen(item["amount"])
    return {
        "worked_days": len(worked_dates),
        "total_minutes": total_minutes,
        "total_display": minutes_to_hhmm(total_minutes),
        "total_amount": total_amount,
        "total_amount_display": format_yen(total_amount),
        "by_work_type": by_work_type,
    }


def _save_attendance_record(request, group, user, redirect_url):
    """勤怠 1 件の追加・修正・削除（メンバーの手動入力と管理者の修正で共通）

    ``record_id`` があればその勤怠を修正し、なければ ``work_date`` の日に追加する。
    """
    record = None
    record_id = request.POST.get("record_id", "")
    if record_id:
        if record_id.isdigit():
            record = AttendanceRecord.objects.filter(group=group, user=user, id=record_id).first()
        if record is None:
            messages.error(request, "対象の勤怠が見つかりません。")
            return redirect(redirect_url)
        work_date = record.work_date
    else:
        work_date = _parse_work_date(request.POST.get("work_date"))
        if work_date is None:
            messages.error(request, "対象の日付が不正です。")
            return redirect(redirect_url)

    if request.POST.get("action") == "delete":
        if record is None:
            messages.error(request, "削除する勤怠が見つかりません。")
            return redirect(redirect_url)
        record.delete()
        messages.success(request, f"{_date_label(work_date)} の勤怠を削除しました。")
        return redirect(redirect_url)

    # フォームの検証中にインスタンスが書き換わるので、変更前の仕事内容を先に控えておく
    previous_work_type_id = record.work_type_id if record else None
    other_records = AttendanceRecord.objects.filter(group=group, user=user, work_date=work_date)
    if record is not None:
        other_records = other_records.exclude(id=record.id)
    form = AttendanceRecordForm(request.POST, instance=record, group=group, other_records=other_records)
    if not form.is_valid():
        messages.error(request, f"勤怠を保存できませんでした。{_form_error_text(form)}")
        return redirect(redirect_url)

    cleaned = form.cleaned_data
    if not cleaned["start_time"] and not cleaned["end_time"]:
        if record is not None:
            record.delete()
            messages.success(request, f"{_date_label(work_date)} の勤怠を削除しました。")
        else:
            messages.info(request, "入力がなかったため、登録しませんでした。")
        return redirect(redirect_url)

    saved = form.save(commit=False)
    saved.group = group
    saved.user = user
    saved.work_date = work_date
    saved.source = AttendanceRecord.MANUAL
    # 時給は仕事内容を選んだ時点のものを使う。仕事内容を変えたときだけ取り直す
    if record is None or saved.work_type_id != previous_work_type_id or saved.hourly_rate is None:
        saved.apply_work_type(saved.work_type)
    saved.save()
    verb = "追加" if record is None else "保存"
    messages.success(request, f"{_date_label(work_date)} の勤怠を{verb}しました。")
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
    today_records = defaultdict(list)
    for record in AttendanceRecord.objects.filter(
        group__in=enabled_groups, user=request.user, work_date=today,
    ):
        today_records[record.group_id].append(record)

    group_cards = []
    for group in enabled_groups:
        records = today_records.get(group.id, [])
        worked_minutes = sum(record.worked_minutes or 0 for record in records)
        group_cards.append({
            "group": group,
            "open_record": next((record for record in records if record.is_working), None),
            "worked_display": minutes_to_hhmm(worked_minutes) if worked_minutes else "",
            "url": reverse("shift:attendance", args=[group.id]),
        })
    return render(request, "shift/attendance_index.html", {
        "group_cards": group_cards,
        "today": today,
        "has_disabled_groups": len(groups) > len(enabled_groups),
    })


def _default_work_type_id(group, user, work_types):
    """新しく勤怠を登録するときに最初から選んでおく仕事内容（前回と同じもの）"""
    active_ids = [work_type.id for work_type in work_types if work_type.is_active]
    if not active_ids:
        return ""
    last_id = (
        AttendanceRecord.objects.filter(group=group, user=user, work_type_id__in=active_ids)
        .order_by("-work_date", "-start_time", "-id")
        .values_list("work_type_id", flat=True)
        .first()
    )
    return last_id or active_ids[0]


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
        redirect_url = "{}?month={}".format(
            reverse("shift:attendance", args=[group.id]), current_month.strftime("%Y-%m"),
        )
        if not setting.allow_manual_input:
            messages.error(request, "このグループでは手動入力が許可されていません。")
            return redirect(redirect_url)
        return _save_attendance_record(request, group, request.user, redirect_url)

    records = _month_records(group, request.user, month_from, month_to)
    records_by_date = _records_by_date(records)
    month_dates = list(_date_range(month_from, month_to))
    opening_schedule_by_date = _opening_schedule_map(group, month_dates)

    days = []
    for work_date in month_dates:
        day_records = records_by_date.get(work_date, [])
        days.append({
            "work_date": work_date,
            "records": day_records,
            # 登録済みの勤怠の下に「この日に勤怠を追加」の行を足すぶん、日付のセルを 1 行伸ばす
            "rowspan": len(day_records) + (1 if setting.allow_manual_input else 0),
            "opening_schedule": opening_schedule_by_date.get(work_date),
            "is_today": work_date == today,
            "is_future": work_date > today,
            "is_saturday": work_date.weekday() == 5,
            "is_sunday": work_date.weekday() == 6,
        })

    today_records = list(
        AttendanceRecord.objects.filter(group=group, user=request.user, work_date=today)
        .select_related("work_type")
    )
    today_worked_minutes = sum(record.worked_minutes or 0 for record in today_records)
    open_record = _open_clock_record(group, request.user, today)
    work_type_context = _work_type_context(group)

    context = {
        "group": group,
        "setting": setting,
        "days": days,
        "totals": _month_totals(records),
        "today": today,
        "today_records": today_records,
        "today_worked_display": minutes_to_hhmm(today_worked_minutes) if today_worked_minutes else "",
        "open_record": open_record,
        "is_working_today": bool(open_record and open_record.work_date == today),
        "default_work_type_id": _default_work_type_id(group, request.user, work_type_context["work_types"]),
        # 追加ダイアログで「その日の登録済みの勤怠」を見せるためのデータ
        "records_payload": {
            work_date.isoformat(): [_record_payload(record) for record in day_records]
            for work_date, day_records in records_by_date.items()
        },
        "default_add_date": today if month_from <= today <= month_to else month_from,
        "month_from": month_from,
        "month_to": month_to,
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "today_month_value": today.strftime("%Y-%m"),
        "is_current_month": current_month == today.replace(day=1),
        **work_type_context,
    }
    return render(request, "shift/attendance.html", context)


def _open_clock_record(group, user, today):
    """退勤が未記録の勤怠を返す（当日になければ前日の夜勤ぶんを探す）"""
    for work_date in (today, today - timedelta(days=1)):
        record = (
            AttendanceRecord.objects.filter(
                group=group, user=user, work_date=work_date,
                start_time__isnull=False, end_time__isnull=True,
            )
            .select_related("work_type")
            .order_by("-start_time", "-id")
            .first()
        )
        if record is not None:
            return record
    return None


def _clock_work_type(group, value):
    """打刻で選んだ仕事内容を返す。(仕事内容, エラーメッセージ)

    仕事内容が1つも用意されていないグループでは選ばなくてよい（仕事内容は None）。
    """
    work_types = WorkType.objects.filter(group=group, is_active=True)
    if not work_types.exists():
        return None, ""
    work_type = work_types.filter(id=value).first() if value.isdigit() else None
    if work_type is None:
        return None, "仕事内容を選択してください。"
    return work_type, ""


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
        if open_record is not None and open_record.work_date == today:
            messages.info(request, f"すでに出勤を記録しています。（{open_record.start_time:%H:%M}〜）")
            return redirect(redirect_url)
        work_type, error = _clock_work_type(group, request.POST.get("work_type", ""))
        if error:
            messages.error(request, error)
            return redirect(redirect_url)
        today_records = AttendanceRecord.objects.filter(group=group, user=request.user, work_date=today)
        if find_overlapping_record(today_records, now_time, None) is not None:
            messages.error(request, "この時刻を含む勤怠がすでに登録されています。一覧表から確認してください。")
            return redirect(redirect_url)
        if open_record is not None:
            messages.warning(request, f"{_date_label(open_record.work_date)} の退勤が記録されていません。"
                                      "一覧表から修正してください。")
        record = AttendanceRecord(
            group=group, user=request.user, work_date=today,
            start_time=now_time, source=AttendanceRecord.CLOCK,
        )
        record.apply_work_type(work_type)
        record.save()
        work_type_label = f" {work_type.name}" if work_type else ""
        messages.success(request, f"出勤を記録しました。（{now_time:%H:%M}{work_type_label}）")
        return redirect(redirect_url)

    if action == "out":
        record = _open_clock_record(group, request.user, today)
        if record is None:
            messages.error(request, "出勤が記録されていません。先に出勤ボタンを押してください。")
            return redirect(redirect_url)
        record.end_time = now_time
        record.source = AttendanceRecord.CLOCK
        record.save()
        messages.success(request, f"退勤を記録しました。（{now_time:%H:%M}／実働 {record.worked_time_display}）")
        return redirect(redirect_url)

    messages.error(request, "操作が不正です。")
    return redirect(redirect_url)


@login_required
def attendance_admin_index(request):
    """勤怠管理のグループ選択（管理者）"""
    return _admin_group_landing(request, "勤怠管理", "shift:attendance_summary")


def _save_work_type(request, group):
    """仕事内容の追加・編集。反映開始日があれば、その日以降の勤怠の時給も書き換える"""
    work_type = WorkType.objects.filter(group=group, id=request.POST.get("work_type_id", "") or 0).first()
    form = WorkTypeForm(request.POST, instance=work_type, group=group)
    if not form.is_valid():
        messages.error(request, f"仕事内容を保存できませんでした。{_form_error_text(form)}")
        return

    saved = form.save(commit=False)
    saved.group = group
    saved.save()

    apply_from = form.cleaned_data.get("apply_from")
    if work_type is None or apply_from is None:
        messages.success(request, f"仕事内容「{saved.name}」を保存しました。")
        return

    # 変更履歴に残すため、update() ではなく1件ずつ保存する
    records = AttendanceRecord.objects.filter(work_type=saved, work_date__gte=apply_from).exclude(
        hourly_rate=saved.hourly_rate,
    )
    updated = 0
    for record in records:
        record.hourly_rate = saved.hourly_rate
        record.save(update_fields=["hourly_rate", "updated_at"])
        updated += 1
    messages.success(
        request,
        f"仕事内容「{saved.name}」を保存し、{_date_label(apply_from)}以降の勤怠 {updated} 件に"
        f"時給 {format_yen(saved.hourly_rate)} を反映しました。",
    )


@login_required
def attendance_settings(request, group_id):
    """管理者向けの勤怠管理設定（機能の有効・無効、入力方法の許可、仕事内容と時給）"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    setting = _attendance_setting(group)

    if request.method == "POST" and request.POST.get("action") == "work_type":
        _save_work_type(request, group)
        return redirect("shift:attendance_settings", group_id=group.id)

    form = AttendanceSettingForm(request.POST or None, instance=setting)
    if request.method == "POST":
        if form.is_valid():
            form.save()
            messages.success(request, "勤怠設定を保存しました。")
            return redirect("shift:attendance_settings", group_id=group.id)
        messages.error(request, "勤怠設定を保存できませんでした。")

    # 勤怠が1件でも登録されている仕事内容は、過去の金額が崩れないように削除させない
    record_counts = dict(
        AttendanceRecord.objects.filter(work_type__group=group)
        .values("work_type")
        .annotate(record_count=Count("id"))
        .values_list("work_type", "record_count")
    )
    work_type_rows = [
        {
            "work_type": work_type,
            "record_count": record_counts.get(work_type.id, 0),
            "delete_url": (
                "" if record_counts.get(work_type.id)
                else reverse("shift:work_type_delete", args=[group.id, work_type.id])
            ),
        }
        for work_type in WorkType.objects.filter(group=group)
    ]

    return render(request, "shift/attendance_settings.html", {
        "group": group,
        "setting": setting,
        "form": form,
        "work_type_rows": work_type_rows,
        "today": _today(),
        "summary_url": reverse("shift:attendance_summary", args=[group.id]),
    })


@login_required
def work_type_delete(request, group_id, work_type_id):
    """仕事内容を削除する（勤怠が登録済みのものは無効化のみ）"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    work_type = get_object_or_404(WorkType, id=work_type_id, group=group)

    if request.method != "POST":
        raise PermissionDenied

    if AttendanceRecord.objects.filter(work_type=work_type).exists():
        messages.error(
            request, f"仕事内容「{work_type.name}」は勤怠が登録されているため削除できません。無効にしてください。",
        )
        return redirect("shift:attendance_settings", group_id=group.id)

    work_type.delete()
    messages.success(request, f"仕事内容「{work_type.name}」を削除しました。")
    return redirect("shift:attendance_settings", group_id=group.id)


def _record_payload(record):
    """勤怠の入力ダイアログに渡す勤怠 1 件ぶんのデータ"""
    return {
        "id": record.id,
        "work_type": record.work_type_id or "",
        "work_type_name": record.work_type.name if record.work_type else "",
        "start": record.start_time.strftime("%H:%M") if record.start_time else "",
        "end": record.end_time.strftime("%H:%M") if record.end_time else "",
        "overnight": record.is_overnight,
        "break_minutes": record.break_minutes or "",
        "note": record.note,
        "worked": record.worked_time_display,
        "amount": record.amount_display,
    }


def _breakdown_table(member_rows):
    """仕事内容別の集計表（メンバー × 仕事内容）"""
    columns = {}
    for row in member_rows:
        for item in row["totals"]["by_work_type"]:
            columns[item["work_type"].id if item["work_type"] else None] = item["work_type"]
    column_keys = sorted(columns, key=lambda key: _work_type_sort_key(columns[key]))

    column_totals = {key: {"minutes": 0, "amount": None} for key in column_keys}
    rows = []
    for row in member_rows:
        items = {
            (item["work_type"].id if item["work_type"] else None): item
            for item in row["totals"]["by_work_type"]
        }
        cells = []
        for key in column_keys:
            item = items.get(key)
            cells.append(item)
            if item is None:
                continue
            column_totals[key]["minutes"] += item["minutes"]
            if item["amount"] is not None:
                column_totals[key]["amount"] = (column_totals[key]["amount"] or 0) + item["amount"]
        rows.append({"member": row["member"], "cells": cells, "totals": row["totals"]})

    footer = [
        {
            "time_display": minutes_to_hhmm(column_totals[key]["minutes"]),
            "amount_display": format_yen(column_totals[key]["amount"]),
        }
        for key in column_keys
    ]
    return {
        "columns": [columns[key] for key in column_keys],
        "rows": rows,
        "footer": footer,
    }


def _summary_members(group):
    """勤怠集計の対象メンバー（有効なユーザーのみ、ユーザー名順）"""
    return [
        membership.user
        for membership in (
            UserGroup.objects.filter(group=group, user__is_active=True)
            .select_related("user")
            .order_by("user__username")
        )
    ]


@login_required
def attendance_summary(request, group_id):
    """管理者向けの勤怠集計（メンバー×日付）。セルから勤怠の追加・修正もできる"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    setting = _attendance_setting(group)

    today = _today()
    month_value = request.POST.get("month", "") if request.method == "POST" else request.GET.get("month", "")
    current_month = _month_from_value(month_value, today)
    month_from, month_to = _month_bounds(current_month)

    members = _summary_members(group)

    if request.method == "POST":
        redirect_url = "{}?month={}".format(
            reverse("shift:attendance_summary", args=[group.id]), current_month.strftime("%Y-%m"),
        )
        if not setting.is_enabled:
            messages.error(request, "勤怠管理が有効になっていません。")
            return redirect(redirect_url)
        target = next(
            (member for member in members if str(member.id) == request.POST.get("user_id", "")), None,
        )
        if target is None:
            messages.error(request, "対象のメンバーが不正です。")
            return redirect(redirect_url)
        return _save_attendance_record(request, group, target, redirect_url)

    month_dates = list(_date_range(month_from, month_to))
    opening_schedule_by_date = _opening_schedule_map(group, month_dates)
    records = defaultdict(list)
    for record in AttendanceRecord.objects.filter(
        group=group, work_date__gte=month_from, work_date__lte=month_to,
    ).select_related("work_type"):
        records[(record.user_id, record.work_date)].append(record)

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
    records_payload = {}
    month_total_minutes = 0
    month_total_amount = 0
    for member in members:
        member_records = []
        cells = []
        for header in date_headers:
            day_records = records.get((member.id, header["work_date"]), [])
            member_records.extend(day_records)
            worked_minutes = sum(record.worked_minutes or 0 for record in day_records)
            if day_records:
                records_payload[f"{member.id}:{header['work_date'].isoformat()}"] = [
                    _record_payload(record) for record in day_records
                ]
            cells.append({
                "work_date": header["work_date"],
                "records": day_records,
                "worked_display": minutes_to_hhmm(worked_minutes) if worked_minutes else "",
                "is_working": any(record.is_working for record in day_records),
                "is_saturday": header["is_saturday"],
                "is_sunday": header["is_sunday"],
                "is_today": header["is_today"],
            })
        totals = _month_totals(member_records)
        month_total_minutes += totals["total_minutes"]
        month_total_amount += totals["total_amount"]
        member_rows.append({
            "member": member,
            "cells": cells,
            "totals": totals,
        })

    context = {
        "group": group,
        "setting": setting,
        "date_headers": date_headers,
        "member_rows": member_rows,
        "records_payload": records_payload,
        "breakdown": _breakdown_table(member_rows),
        "month_total_display": minutes_to_hhmm(month_total_minutes),
        "month_total_amount_display": format_yen(month_total_amount),
        "current_month": current_month,
        "current_month_value": current_month.strftime("%Y-%m"),
        "prev_month": _add_month(current_month, -1),
        "next_month": _add_month(current_month, 1),
        "today_month_value": today.strftime("%Y-%m"),
        "is_current_month": current_month == today.replace(day=1),
        "settings_url": reverse("shift:attendance_settings", args=[group.id]),
        "csv_url": reverse("shift:attendance_summary_csv", args=[group.id]),
        **_work_type_context(group),
    }
    return render(request, "shift/attendance_summary.html", context)


_CSV_KINDS = {
    "records": "勤怠明細",
    "members": "勤怠集計",
}
_WEEKDAY_LABELS = "月火水木金土日"


def _csv_text(value):
    """表計算ソフトで数式として実行されないように、記号で始まる文字列の先頭に ' を付ける"""
    value = str(value or "")
    if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _csv_time(value):
    return value.strftime("%H:%M") if value else ""


def _csv_number(value):
    """None は空欄、それ以外は数値のまま（円や桁区切りは付けない）"""
    return "" if value is None else value


def _csv_record_rows(records, show_amount):
    header = ["勤務日", "曜日", "ユーザー名", "氏名", "仕事内容", "出勤時刻", "退勤時刻", "日またぎ",
              "休憩（分）", "実働（時:分）", "実働（分）"]
    if show_amount:
        header += ["時給（円）", "金額（円）"]
    header += ["入力方法", "メモ"]
    yield header

    for record in records:
        row = [
            record.work_date.isoformat(),
            _WEEKDAY_LABELS[record.work_date.weekday()],
            _csv_text(record.user.username),
            _csv_text(record.user.username_jp),
            _csv_text(record.work_type.name if record.work_type else ""),
            _csv_time(record.start_time),
            _csv_time(record.end_time),
            "○" if record.is_overnight else "",
            record.break_minutes,
            record.worked_time_display,
            _csv_number(record.worked_minutes),
        ]
        if show_amount:
            row += [_csv_number(record.hourly_rate), _csv_number(record.amount)]
        row += [record.get_source_display(), _csv_text(record.note)]
        yield row


def _csv_member_rows(members, records, work_types, show_amount):
    records_by_user = defaultdict(list)
    for record in records:
        records_by_user[record.user_id].append(record)
    member_totals = [(member, _month_totals(records_by_user.get(member.id, []))) for member in members]

    # 列が月ごとに変わらないように、有効な仕事内容は勤怠がなくても列を出す。
    # 仕事内容を使っていないグループでは内訳の列は出さない
    columns = {work_type.id: work_type for work_type in work_types if work_type.is_active}
    for _member, totals in member_totals if work_types else ():
        for item in totals["by_work_type"]:
            columns[item["work_type"].id if item["work_type"] else None] = item["work_type"]
    column_keys = sorted(columns, key=lambda key: _work_type_sort_key(columns[key]))

    header = ["ユーザー名", "氏名", "勤務日数", "実働（時:分）", "実働（分）"]
    if show_amount:
        header.append("金額（円）")
    for key in column_keys:
        name = columns[key].name if columns[key] else "未設定"
        header += [f"{name} 実働（分）"] + ([f"{name} 金額（円）"] if show_amount else [])
    yield [_csv_text(label) for label in header]

    for member, totals in member_totals:
        row = [
            _csv_text(member.username),
            _csv_text(member.username_jp),
            totals["worked_days"],
            totals["total_display"] or "0:00",
            totals["total_minutes"],
        ]
        if show_amount:
            row.append(totals["total_amount"])
        items = {
            (item["work_type"].id if item["work_type"] else None): item
            for item in totals["by_work_type"]
        }
        for key in column_keys:
            item = items.get(key)
            row.append(item["minutes"] if item else 0)
            if show_amount:
                row.append(_csv_number(item["amount"]) if item else 0)
        yield row


@login_required
def attendance_summary_csv(request, group_id):
    """勤怠集計の CSV ダウンロード（kind=records: 勤怠の明細、kind=members: メンバー別の集計）

    Excel でそのまま開けるように UTF-8（BOM 付き）で出力する。対象は勤怠集計の画面と同じメンバー。
    """
    group = _get_group_for_user(request, group_id, require_admin=True)
    kind = request.GET.get("kind", "records")
    if kind not in _CSV_KINDS:
        raise Http404

    current_month = _month_from_value(request.GET.get("month", ""), _today())
    month_from, month_to = _month_bounds(current_month)
    members = _summary_members(group)
    member_order = {member.id: index for index, member in enumerate(members)}
    records = sorted(
        AttendanceRecord.objects.filter(
            group=group, user__in=members, work_date__gte=month_from, work_date__lte=month_to,
        ).select_related("user", "work_type"),
        key=lambda record: (member_order[record.user_id], record.work_date,
                            record.start_time is None, record.start_time, record.id),
    )
    work_types = list(WorkType.objects.filter(group=group))
    show_amount = bool(work_types)

    if kind == "records":
        rows = _csv_record_rows(records, show_amount)
    else:
        rows = _csv_member_rows(members, records, work_types, show_amount)

    filename = get_valid_filename(f"{_CSV_KINDS[kind]}_{group.name_jp}_{current_month:%Y-%m}.csv")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = content_disposition_header(True, filename)
    response.write("\ufeff")  # Excel で文字化けしないように BOM を付ける
    csv.writer(response).writerows(rows)
    return response
