"""確定シフト（管理者によるシフトの割り当てと公開、メンバー向けの閲覧）"""

from collections import defaultdict
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from custom_auth.models import UserGroup
from shift import compare, slack
from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    ShiftAssignment,
    ShiftEntry,
    TimeSlot,
    minutes_to_hhmm,
    staffing_status,
)
from shift.views import (
    _active_time_slots,
    _admin_group_landing,
    _date_range,
    _get_group_for_user,
    _month_bounds,
    _month_from_value,
    _month_nav_context,
    _opening_schedule_map,
    _required_counts,
    _schedule_legend,
    _today,
)

_HOPE_SYMBOLS = {
    ShiftEntry.AVAILABLE: "○",
    ShiftEntry.MAYBE: "△",
    ShiftEntry.UNAVAILABLE: "×",
}
_NOTE_MAX_LENGTH = ShiftAssignment._meta.get_field("note").max_length


def _group_members(group):
    """割り当ての対象になるメンバー（有効なユーザーを表示名順）"""
    return sorted(
        (
            membership.user
            for membership in UserGroup.objects.filter(group=group, user__is_active=True).select_related("user")
        ),
        key=lambda user: (user.get_display_name(), user.username),
    )


def _slot_minutes(time_slot):
    start = time_slot.start_time.hour * 60 + time_slot.start_time.minute
    end = time_slot.end_time.hour * 60 + time_slot.end_time.minute
    return end - start


def _time_slots_for_month(group, month_from, month_to, published_only=False):
    """表示する勤務時間。無効にした勤務時間でも、その月に割り当てが残っていれば含める"""
    time_slots = _active_time_slots(group)
    assignments = ShiftAssignment.objects.filter(
        group=group, work_date__gte=month_from, work_date__lte=month_to, time_slot__is_active=False
    )
    if published_only:
        assignments = assignments.filter(is_published=True)
    inactive = list(TimeSlot.objects.filter(id__in=assignments.values("time_slot_id")))
    return sorted(time_slots + inactive, key=lambda slot: (slot.start_time, slot.name))


def _slot_legend(time_slots):
    return [
        {
            "index": index,
            "time_slot": time_slot,
            "time_range": "%s〜%s" % (time_slot.start_time.strftime("%H:%M"), time_slot.end_time.strftime("%H:%M")),
        }
        for index, time_slot in enumerate(time_slots, start=1)
    ]


def _date_headers(group, month_from, month_to, today):
    dates = list(_date_range(month_from, month_to))
    opening_schedules = _opening_schedule_map(group, dates)
    headers = []
    for work_date in dates:
        schedule = opening_schedules.get(work_date)
        headers.append(
            {
                "work_date": work_date,
                "is_saturday": work_date.weekday() == 5,
                "is_sunday": work_date.weekday() == 6,
                "is_today": work_date == today,
                "opening_schedule": schedule,
                "schedule_color": schedule.schedule_type.color if schedule else "",
                "is_blocked": schedule is not None and schedule.schedule_type.blocks_shift_input,
            }
        )
    return headers


def _month_redirect(group, current_month):
    return redirect("%s?month=%s" % (reverse("shift:assignment", args=[group.id]), current_month.strftime("%Y-%m")))


def _save_cell(request, group, members, current_month):
    """1 人 × 1 日の割り当てを、選択された勤務時間の組み合わせに置き換える"""
    target = next((member for member in members if str(member.id) == request.POST.get("user_id", "")), None)
    try:
        work_date = parse_date(request.POST.get("work_date", "") or "")
    except ValueError:  # 2月30日のように形式は正しくても存在しない日付
        work_date = None
    if target is None or work_date is None:
        messages.error(request, "対象のメンバーまたは日付が不正です。")
        return _month_redirect(group, current_month)

    try:
        selected_ids = {int(value) for value in request.POST.getlist("time_slot")}
    except ValueError:
        messages.error(request, "勤務時間の指定が不正です。")
        return _month_redirect(group, current_month)
    note = request.POST.get("note", "").strip()[:_NOTE_MAX_LENGTH]

    with transaction.atomic():
        # 対象メンバーと操作ユーザーの所属をロックしてから確かめ直す。メンバーの削除・脱退
        # （custom_auth.views._remove_membership）や、同じメンバーへの同時保存と直列化するため
        locked = {
            membership.user_id: membership
            for membership in UserGroup.objects.select_for_update().filter(
                group=group, user_id__in={target.id, request.user.id}
            )
        }
        actor = locked.get(request.user.id)
        if target.id not in locked or not (request.user.is_staff or (actor and actor.is_admin)):
            messages.error(request, "対象のメンバーがグループに所属していないか、管理者権限がありません。")
            return _month_redirect(group, current_month)

        existing = {
            assignment.time_slot_id: assignment
            for assignment in ShiftAssignment.objects.select_for_update().filter(
                group=group, user=target, work_date=work_date
            )
        }
        # 新しく割り当てられるのは有効な勤務時間だけ。無効にした勤務時間は、割り当て済みのものを残す場合だけ受け付ける
        allowed_ids = set(
            TimeSlot.objects.filter(group=group, is_active=True, id__in=selected_ids).values_list("id", flat=True)
        ) | (selected_ids & set(existing))
        if selected_ids - allowed_ids:
            messages.error(request, "選択できない勤務時間が含まれています。")
            return _month_redirect(group, current_month)

        removed = [assignment.id for slot_id, assignment in existing.items() if slot_id not in selected_ids]
        ShiftAssignment.objects.filter(id__in=removed).delete()
        for slot_id in selected_ids:
            assignment = existing.get(slot_id)
            if assignment is None:
                ShiftAssignment.objects.create(
                    group=group, user=target, work_date=work_date, time_slot_id=slot_id, note=note
                )
            elif assignment.note != note:
                assignment.note = note
                assignment.save(update_fields=["note", "updated_at"])

    messages.success(
        request,
        "%s さんの %d/%d のシフトを保存しました。" % (target.get_display_name(), work_date.month, work_date.day),
    )
    return _month_redirect(group, current_month)


def _publish(request, group, month_from, month_to, current_month):
    """表示中の月の未公開の割り当てを公開する"""
    now = timezone.now()
    with transaction.atomic():
        # 画面を開いたあとに降格・削除された管理者が公開できないよう、所属をロックして確かめ直す
        if not request.user.is_staff and not (
            UserGroup.objects.select_for_update().filter(group=group, user=request.user, is_admin=True).exists()
        ):
            messages.error(request, "このグループの管理者権限がありません。")
            return _month_redirect(group, current_month)
        # update() では変更履歴（simple_history）が残らないので、1 件ずつ保存する
        targets = list(
            ShiftAssignment.objects.select_for_update().filter(
                group=group,
                work_date__gte=month_from,
                work_date__lte=month_to,
                is_published=False,
            )
        )
        for assignment in targets:
            assignment.is_published = True
            assignment.published_at = now
            assignment.save(update_fields=["is_published", "published_at", "updated_at"])
    count = len(targets)
    if count:
        messages.success(request, "%d件の割り当てを公開しました。" % count)
        slack.notify_shift_published(group, count, month_from, month_to)
    else:
        messages.info(request, "公開していない割り当てはありません。")
    return _month_redirect(group, current_month)


@login_required
def assignment_index(request):
    """シフト作成のグループ選択"""
    return _admin_group_landing(request, "シフト作成", "shift:assignment")


@login_required
def assignment(request, group_id):
    """管理者向けのシフト作成（メンバー × 日付の表で、希望を見ながら勤務時間を割り当てる）"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    today = _today()
    month_value = request.POST.get("month", "") if request.method == "POST" else request.GET.get("month", "")
    current_month = _month_from_value(month_value, today)
    month_from, month_to = _month_bounds(current_month)
    members = _group_members(group)

    if request.method == "POST":
        if request.POST.get("action") == "publish":
            return _publish(request, group, month_from, month_to, current_month)
        return _save_cell(request, group, members, current_month)

    time_slots = _time_slots_for_month(group, month_from, month_to)
    date_headers = _date_headers(group, month_from, month_to, today)

    # 下書きのまま提出されていない希望は提出状況と同じく扱わない
    hopes = {
        (entry.user_id, entry.work_date, entry.time_slot_id): entry
        for entry in ShiftEntry.objects.filter(
            group=group, work_date__gte=month_from, work_date__lte=month_to, is_draft=False
        )
    }
    assignments = defaultdict(dict)
    unpublished_count = 0
    for item in ShiftAssignment.objects.filter(
        group=group, time_slot__group=group, work_date__gte=month_from, work_date__lte=month_to
    ):
        assignments[(item.user_id, item.work_date)][item.time_slot_id] = item
        if not item.is_published:
            unpublished_count += 1

    day_counts = defaultdict(int)
    member_rows = []
    cells_payload = {}
    for member in members:
        cells = []
        assigned_slots = 0
        assigned_minutes = 0
        for header in date_headers:
            work_date = header["work_date"]
            day_assignments = assignments.get((member.id, work_date), {})
            slot_cells = []
            note = ""
            for time_slot in time_slots:
                hope = hopes.get((member.id, work_date, time_slot.id))
                assigned = day_assignments.get(time_slot.id)
                if assigned is None and not time_slot.is_active:
                    # 無効にした勤務時間は、割り当てが残っている日だけ表示する
                    slot_cells.append({"time_slot": time_slot, "hidden": True})
                    continue
                if assigned:
                    assigned_slots += 1
                    assigned_minutes += _slot_minutes(time_slot)
                    day_counts[(work_date, time_slot.id)] += 1
                    note = note or assigned.note
                slot_cells.append(
                    {
                        "time_slot": time_slot,
                        "hope": hope,
                        "hope_symbol": _HOPE_SYMBOLS.get(hope.status, "") if hope else "",
                        "assignment": assigned,
                        # 「勤務不可」と答えた時間帯に割り当てたときは気付けるようにする
                        "is_conflict": bool(assigned and hope and hope.status == ShiftEntry.UNAVAILABLE),
                    }
                )
            if day_assignments or any(cell.get("hope") for cell in slot_cells):
                cells_payload["%d:%s" % (member.id, work_date.isoformat())] = {
                    "assigned": sorted(day_assignments),
                    "hopes": {
                        str(cell["time_slot"].id): cell["hope"].status for cell in slot_cells if cell.get("hope")
                    },
                    "note": note,
                }
            cells.append({**header, "slot_cells": slot_cells, "note": note})
        member_rows.append(
            {
                "member": member,
                "cells": cells,
                "assigned_slots": assigned_slots,
                "assigned_time": minutes_to_hhmm(assigned_minutes) or "0:00",
            }
        )

    # 日別の割り当て人数を必要人数と比べる
    required_counts = _required_counts(group, date_headers, time_slots)
    day_totals = []
    staffing = {"short": 0, "over": 0}
    for header in date_headers:
        slot_counts = []
        for time_slot in time_slots:
            key = (header["work_date"], time_slot.id)
            status = staffing_status(day_counts[key], required_counts[key])
            if status in staffing:
                staffing[status] += 1
            slot_counts.append(
                {
                    "time_slot": time_slot,
                    "assigned": day_counts[key],
                    "required": required_counts[key],
                    "status": status,
                }
            )
        day_totals.append({**header, "slot_counts": slot_counts})

    context = {
        "group": group,
        "members": members,
        "time_slots": time_slots,
        "slot_options": [
            {"id": time_slot.id, "label": time_slot.display_label, "is_active": time_slot.is_active}
            for time_slot in time_slots
        ],
        "slot_legend": _slot_legend(time_slots),
        "schedule_legend": _schedule_legend(group),
        "date_headers": date_headers,
        "member_rows": member_rows,
        "day_totals": day_totals,
        "cells_payload": cells_payload,
        "unpublished_count": unpublished_count,
        "staffing": staffing,
        "has_requirements": any(required_counts.values()),
        "note_max_length": _NOTE_MAX_LENGTH,
        **_month_nav_context(current_month, today),
    }
    return render(request, "shift/assignment.html", context)


@login_required
def assignment_member(request, group_id):
    """メンバー向けの確定シフト（自分のシフト一覧と、グループ全員の公開済みシフト表）"""
    group = _get_group_for_user(request, group_id)
    today = _today()
    current_month = _month_from_value(request.GET.get("month", ""), today)
    month_from, month_to = _month_bounds(current_month)

    published = list(
        ShiftAssignment.objects.filter(
            group=group,
            time_slot__group=group,
            work_date__gte=month_from,
            work_date__lte=month_to,
            is_published=True,
        ).select_related("time_slot")
    )
    time_slots = _time_slots_for_month(group, month_from, month_to, published_only=True)
    slot_index = {time_slot.id: index for index, time_slot in enumerate(time_slots, start=1)}
    date_headers = _date_headers(group, month_from, month_to, today)

    by_cell = defaultdict(list)
    for item in published:
        by_cell[(item.user_id, item.work_date)].append(item)

    my_days = []
    my_minutes = 0
    for header in date_headers:
        items = by_cell.get((request.user.id, header["work_date"]), [])
        if items:
            my_minutes += sum(_slot_minutes(item.time_slot) for item in items)
            my_days.append({**header, "assignments": items, "note": next((i.note for i in items if i.note), "")})

    member_rows = []
    for member in _group_members(group):
        member_rows.append(
            {
                "member": member,
                "is_me": member.id == request.user.id,
                "cells": [
                    {
                        **header,
                        "indexes": [
                            {"index": slot_index[item.time_slot_id], "time_slot": item.time_slot, "note": item.note}
                            for item in by_cell.get((member.id, header["work_date"]), [])
                        ],
                    }
                    for header in date_headers
                ],
            }
        )

    context = {
        "group": group,
        "is_member": UserGroup.objects.filter(group=group, user=request.user).exists(),
        "my_days": my_days,
        "my_total_time": minutes_to_hhmm(my_minutes) or "0:00",
        "slot_legend": _slot_legend(time_slots),
        "date_headers": date_headers,
        "member_rows": member_rows,
        "has_published": bool(published),
        **_month_nav_context(current_month, today),
    }
    return render(request, "shift/assignment_member.html", context)


GRACE_OPTIONS = (0, 5, 10, 15, 30)
DEFAULT_GRACE_MINUTES = 5


def _grace_minutes(value):
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GRACE_MINUTES
    return minutes if minutes in GRACE_OPTIONS else DEFAULT_GRACE_MINUTES


@login_required
def assignment_compare(request, group_id):
    """管理者向けの予実比較（公開済みの確定シフトと勤怠の食い違い）"""
    group = _get_group_for_user(request, group_id, require_admin=True)
    today = _today()
    current_month = _month_from_value(request.GET.get("month", ""), today)
    month_from, month_to = _month_bounds(current_month)
    grace = _grace_minutes(request.GET.get("grace"))

    # メンバーに見せていない（未公開の）割り当ては予定として扱わない
    # 月末の夜から続く勤務が翌月 1 日の早朝のシフトに重なることがあるので、翌日のシフトも照合に使う
    nearby_assignments = list(
        ShiftAssignment.objects.filter(
            group=group,
            time_slot__group=group,
            work_date__gte=month_from,
            work_date__lte=month_to + timedelta(days=1),
            is_published=True,
        ).select_related("time_slot")
    )
    assignments = [assignment for assignment in nearby_assignments if assignment.work_date <= month_to]
    # 前月末の夜から続く勤務が月初の早朝のシフトに重なることがあるので、前日の勤怠も比べる
    nearby_records = list(
        AttendanceRecord.objects.filter(
            group=group, work_date__gte=month_from - timedelta(days=1), work_date__lte=month_to
        ).select_related("work_type")
    )
    records = [record for record in nearby_records if record.work_date >= month_from]
    discrepancies = compare.compare(nearby_assignments, nearby_records, today, grace, month_from, month_to)

    # 食い違いの集計は、グループから外れた人も含めて予定か実績のある全員を対象にする
    member_ids = set(UserGroup.objects.filter(group=group).values_list("user_id", flat=True))
    user_ids = {item.user_id for item in assignments} | {item.user_id for item in records}
    users = {user.id: user for user in get_user_model().objects.filter(id__in=user_ids)}
    for item in discrepancies:
        item.user = users[item.user_id]

    rows = {}
    for user_id in user_ids:
        rows[user_id] = {
            "user": users[user_id],
            "is_former_member": user_id not in member_ids,
            "shift_count": 0,
            "counts": dict.fromkeys(compare.KIND_ORDER, 0),
        }
    for item in assignments:
        rows[item.user_id]["shift_count"] += 1
    for item in discrepancies:
        rows[item.user_id]["counts"][item.kind] += 1
    member_rows = sorted(rows.values(), key=lambda row: (row["user"].get_display_name(), row["user"].username))
    for row in member_rows:
        row["count_list"] = [row["counts"][kind] for kind in compare.KIND_ORDER]

    context = {
        "group": group,
        "grace": grace,
        "grace_options": GRACE_OPTIONS,
        "kinds": [{"key": kind, "label": compare.KIND_LABELS[kind]} for kind in compare.KIND_ORDER],
        "totals": [
            {"key": kind, "label": compare.KIND_LABELS[kind], "count": sum(1 for d in discrepancies if d.kind == kind)}
            for kind in compare.KIND_ORDER
        ],
        "discrepancies": discrepancies,
        "member_rows": member_rows,
        "has_assignments": bool(assignments),
        "attendance_enabled": AttendanceSetting.objects.filter(group=group, is_enabled=True).exists(),
        **_month_nav_context(current_month, today),
    }
    return render(request, "shift/assignment_compare.html", context)
