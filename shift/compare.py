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


def _slot_range(time_slot):
    start = time_slot.start_time.hour * 60 + time_slot.start_time.minute
    end = time_slot.end_time.hour * 60 + time_slot.end_time.minute
    return start, end


def _overlaps(record_range, slot_start, slot_end):
    start, end = record_range
    if end is None:
        # 勤務中（退勤が未記録）の勤怠は、出勤した時刻からずっと続いているものとして扱う
        return start < slot_end
    return start < slot_end and end > slot_start


def compare_day(user_id, work_date, assignments, records, today, grace_minutes):
    """1 人 × 1 日の確定シフトと勤怠を比べて、食い違いの一覧を返す

    - 欠勤: 時間帯に重なる勤怠が 1 件もない（今日より前の日だけ判定する）
    - 遅刻: 重なる勤怠の最初の出勤が、シフト開始 + 猶予より遅い
    - 早退: 重なる勤怠の最後の退勤が、シフト終了 - 猶予より早い（勤務中の勤怠があれば判定しない）
    - シフト外勤務: その日のどの確定シフトにも重ならない勤怠
    """
    # minute_range は勤務日の 0:00 起点の (出勤, 退勤) の分。出勤がない勤怠は比べようがないので除く
    ranges = [(record, record.minute_range) for record in records if record.minute_range is not None]
    matched_records = set()
    results = []

    for assignment in sorted(assignments, key=lambda item: item.time_slot.start_time):
        slot_start, slot_end = _slot_range(assignment.time_slot)
        overlapping = [(record, span) for record, span in ranges if _overlaps(span, slot_start, slot_end)]
        matched_records.update(record.id for record, _span in overlapping)
        if not overlapping:
            if work_date < today:
                results.append(Discrepancy(ABSENT, user_id, work_date, assignment=assignment))
            continue

        first_start = min(span[0] for _record, span in overlapping)
        if first_start > slot_start + grace_minutes:
            results.append(
                Discrepancy(LATE, user_id, work_date, assignment=assignment, minutes=first_start - slot_start)
            )

        ends = [span[1] for _record, span in overlapping]
        if None not in ends and max(ends) < slot_end - grace_minutes:
            results.append(Discrepancy(EARLY, user_id, work_date, assignment=assignment, minutes=slot_end - max(ends)))

    for record, _span in ranges:
        if record.id not in matched_records:
            results.append(Discrepancy(UNSCHEDULED, user_id, work_date, record=record))
    return results


def compare(assignments, records, today, grace_minutes):
    """確定シフトと勤怠をメンバー × 日付ごとに突き合わせ、食い違いを日付・種類の順に返す"""
    assignments_by_day = defaultdict(list)
    for assignment in assignments:
        assignments_by_day[(assignment.user_id, assignment.work_date)].append(assignment)
    records_by_day = defaultdict(list)
    for record in records:
        records_by_day[(record.user_id, record.work_date)].append(record)

    results = []
    for user_id, work_date in set(assignments_by_day) | set(records_by_day):
        results.extend(
            compare_day(
                user_id,
                work_date,
                assignments_by_day.get((user_id, work_date), []),
                records_by_day.get((user_id, work_date), []),
                today,
                grace_minutes,
            )
        )
    kind_rank = {kind: index for index, kind in enumerate(KIND_ORDER)}
    results.sort(key=lambda item: (item.work_date, item.user_id, kind_rank[item.kind]))
    return results
