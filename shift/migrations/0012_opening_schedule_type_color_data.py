from django.db import migrations


# 表示色が設定できるようになる前に画面で使っていたパレット
LEGACY_PALETTE = [
    "#0d6efd",  # blue
    "#198754",  # green
    "#fd7e14",  # orange
    "#6f42c1",  # purple
    "#20c997",  # teal
    "#d63384",  # pink
    "#ffc107",  # yellow
    "#0dcaf0",  # cyan
]
LEGACY_BLOCKED_COLOR = "#dc3545"  # シフト入力不可の区分は赤固定だった


def fill_legacy_colors(apps, schema_editor):
    """既存の開講区分に、これまで画面に出ていた色をそのまま保存する"""
    OpeningScheduleType = apps.get_model("shift", "OpeningScheduleType")

    for schedule_type in OpeningScheduleType.objects.all():
        if schedule_type.blocks_shift_input:
            color = LEGACY_BLOCKED_COLOR
        else:
            color = LEGACY_PALETTE[schedule_type.id % len(LEGACY_PALETTE)]
        OpeningScheduleType.objects.filter(id=schedule_type.id).update(color=color)


class Migration(migrations.Migration):

    dependencies = [
        ("shift", "0011_opening_schedule_type_color"),
    ]

    operations = [
        # 逆方向は色の列ごと削除されるため noop
        migrations.RunPython(fill_legacy_colors, migrations.RunPython.noop),
    ]
