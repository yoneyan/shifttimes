"""確定シフト（予定）と勤怠（実績）の突き合わせ"""

from collections import defaultdict
from dataclasses import dataclass

ABSENT = "absent"
LATE = "late"
EARLY = "early"
UNSCHEDULED = "unscheduled"

KIND_LABELS = {
    ABSENT: "欠勤",
    LATE: "遅刻",
    EARLY: "早退",
    UNSCHEDULED: "シフト外勤務",
}
KIND_ORDER = (ABSENT, LATE, EARLY, UNSCHEDULED)


@dataclass
class Discrepancy:
    kind: str
    user_id: int
    work_date: object
    assignment: object = None
    record: object = None
    minutes: int = 0

    @property
    def label(self):
        return KIND_LABELS[self.kind]


_MINUTES_PER_DAY = 24 * 60


def _timeline(work_date, minutes):
    """日付をまたいで比べられるよう、勤務日と 0:00 起点の分を通しの分に直す"""
    return work_date.toordinal() * _MINUTES_PER_DAY + minutes


def _slot_span(assignment):
    time_slot = assignment.time_slot
    start = time_slot.start_time.hour * 60 + time_slot.start_time.minute
    end = time_slot.end_time.hour * 60 + time_slot.end_time.minute
    return _timeline(assignment.work_date, start), _timeline(assignment.work_date, end)


def _record_span(record):
    """勤怠の (出勤, 退勤) を通しの分で返す。比べようのない勤怠は None

    出勤がない勤怠と、出勤と退勤が同じ時刻（勤務した時間がない）勤怠は対象にしない。
    退勤が未記録（勤務中）なら退勤は None。日をまたぐ退勤は翌日の時刻になる。
    """
    span = record.minute_range
    if span is None:
        return None
    start, end = span
    if end is not None and end <= start:
        return None
    return _timeline(record.work_date, start), None if end is None else _timeline(record.work_date, end)


def _overlaps(record_span, slot_start, slot_end):
    start, end = record_span
    if end is None:
        # 勤務中（退勤が未記録）の勤怠は、出勤した時刻からずっと続いているものとして扱う
        return start < slot_end
    return start < slot_end and end > slot_start


def _compare_assignment(assignment, record_spans, today, grace_minutes):
    """1 件の確定シフトを、そのメンバーの勤怠と比べる。(食い違い, 重なった勤怠の id) を返す"""
    slot_start, slot_end = _slot_span(assignment)
    overlapping = [(record, span) for record, span in record_spans if _overlaps(span, slot_start, slot_end)]
    matched = {record.id for record, _span in overlapping}
    user_id, work_date = assignment.user_id, assignment.work_date
    if not overlapping:
        if work_date < today:
            return [Discrepancy(ABSENT, user_id, work_date, assignment=assignment)], matched
        return [], matched

    results = []
    first_start = min(span[0] for _record, span in overlapping)
    if first_start > slot_start + grace_minutes:
        results.append(Discrepancy(LATE, user_id, work_date, assignment=assignment, minutes=first_start - slot_start))
    ends = [span[1] for _record, span in overlapping]
    if None not in ends and max(ends) < slot_end - grace_minutes:
        results.append(Discrepancy(EARLY, user_id, work_date, assignment=assignment, minutes=slot_end - max(ends)))
    return results, matched


def compare(assignments, records, today, grace_minutes, date_from=None, date_to=None):
    """確定シフトと勤怠を突き合わせ、食い違いを日付・種類の順に返す

    - 欠勤: 時間帯に重なる勤怠が 1 件もない（今日より前の日だけ判定する）
    - 遅刻: 重なる勤怠の最初の出勤が、シフト開始 + 猶予より遅い
    - 早退: 重なる勤怠の最後の退勤が、シフト終了 - 猶予より早い（勤務中の勤怠があれば判定しない）
    - シフト外勤務: どの確定シフトにも重ならない勤怠

    勤怠は日付をまたいだ通しの時間で比べるので、前日の夜から続く勤務も翌日の早朝のシフトに重なる。
    そのために records には対象期間の前日の勤怠も渡し、シフト外勤務は date_from〜date_to の勤務日の
    勤怠だけを返す（未指定なら渡した勤怠すべて）。
    """
    spans_by_user = defaultdict(list)
    for record in records:
        span = _record_span(record)
        if span is not None:
            spans_by_user[record.user_id].append((record, span))

    results = []
    matched = set()
    for assignment in assignments:
        found, matched_ids = _compare_assignment(
            assignment, spans_by_user.get(assignment.user_id, []), today, grace_minutes
        )
        results.extend(found)
        matched |= matched_ids

    for spans in spans_by_user.values():
        for record, _span in spans:
            in_range = (date_from is None or record.work_date >= date_from) and (
                date_to is None or record.work_date <= date_to
            )
            if in_range and record.id not in matched:
                results.append(Discrepancy(UNSCHEDULED, record.user_id, record.work_date, record=record))

    kind_rank = {kind: index for index, kind in enumerate(KIND_ORDER)}
    results.sort(key=lambda item: (item.work_date, item.user_id, kind_rank[item.kind]))
    return results
