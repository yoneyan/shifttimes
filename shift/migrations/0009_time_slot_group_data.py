from django.db import migrations


def split_time_slots_by_group(apps, schema_editor):
    """グループ共通だった勤務時間をグループごとに複製し、シフト希望を付け替える"""
    Group = apps.get_model("custom_auth", "Group")
    TimeSlot = apps.get_model("shift", "TimeSlot")
    HistoricalTimeSlot = apps.get_model("shift", "HistoricalTimeSlot")
    ShiftEntry = apps.get_model("shift", "ShiftEntry")
    HistoricalShiftEntry = apps.get_model("shift", "HistoricalShiftEntry")

    legacy_slots = list(TimeSlot.objects.filter(group__isnull=True).order_by("start_time", "id"))
    if not legacy_slots:
        return

    groups = list(Group.objects.order_by("id"))
    if not groups:
        # グループが1つもなければ勤務時間を持たせる先がないので、そのまま削除する
        TimeSlot.objects.filter(group__isnull=True).delete()
        HistoricalTimeSlot.objects.filter(group__isnull=True).delete()
        return

    # (group, name) を一意にするため、同名の時間帯には連番を付ける
    unique_names = {}
    used_names = set()
    for slot in legacy_slots:
        name = slot.name
        suffix = 2
        while name in used_names:
            name = "%s (%d)" % (slot.name[:90], suffix)
            suffix += 1
        used_names.add(name)
        unique_names[slot.id] = name

    keep_group = groups[0]
    for slot in legacy_slots:
        for group in groups[1:]:
            copied = TimeSlot.objects.create(
                group=group,
                name=unique_names[slot.id],
                start_time=slot.start_time,
                end_time=slot.end_time,
                is_active=slot.is_active,
                created_at=slot.created_at,
            )
            ShiftEntry.objects.filter(group=group, time_slot=slot).update(time_slot=copied)
            HistoricalShiftEntry.objects.filter(group_id=group.id, time_slot_id=slot.id).update(
                time_slot_id=copied.id,
            )
        slot.group = keep_group
        slot.name = unique_names[slot.id]
        slot.save(update_fields=["group", "name"])

    HistoricalTimeSlot.objects.filter(group__isnull=True).update(group=keep_group)


class Migration(migrations.Migration):

    dependencies = [
        ("shift", "0008_timeslot_group"),
    ]

    operations = [
        # 逆方向はグループ共通に戻せない（複製した勤務時間が残る）ため noop
        migrations.RunPython(split_time_slots_by_group, migrations.RunPython.noop),
    ]
