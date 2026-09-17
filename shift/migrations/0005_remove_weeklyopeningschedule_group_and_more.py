import datetime

from django.db import migrations
from django.utils import timezone


MATERIALIZE_MONTHS_AHEAD = 6


def materialize_weekly_schedules(apps, schema_editor):
    """曜日別の基本スケジュールを、日付別スケジュールの実データに変換する

    曜日別設定の廃止にあたり、これまで暗黙に適用されていた開講区分が
    消えてしまわないよう、当月初日から MATERIALIZE_MONTHS_AHEAD ヶ月先までを
    日付別に書き出す。それ以降は「曜日一括設定」で登録してもらう。
    """
    WeeklyOpeningSchedule = apps.get_model("shift", "WeeklyOpeningSchedule")
    DateOpeningSchedule = apps.get_model("shift", "DateOpeningSchedule")

    weekly_schedules = list(WeeklyOpeningSchedule.objects.filter(is_active=True))
    if not weekly_schedules:
        return

    today = timezone.now().date()
    date_from = today.replace(day=1)
    end_month_index = date_from.year * 12 + date_from.month - 1 + MATERIALIZE_MONTHS_AHEAD
    date_to = datetime.date(end_month_index // 12, end_month_index % 12 + 1, 1) - datetime.timedelta(days=1)

    by_group_weekday = {}
    for weekly in weekly_schedules:
        by_group_weekday[(weekly.group_id, weekly.weekday)] = weekly

    existing_dates = set(
        DateOpeningSchedule.objects.filter(work_date__gte=date_from, work_date__lte=date_to)
        .values_list("group_id", "work_date")
    )

    new_rows = []
    work_date = date_from
    while work_date <= date_to:
        for (group_id, weekday), weekly in by_group_weekday.items():
            if weekday != work_date.weekday():
                continue
            if (group_id, work_date) in existing_dates:
                continue
            new_rows.append(DateOpeningSchedule(
                group_id=group_id,
                work_date=work_date,
                schedule_type_id=weekly.schedule_type_id,
                note=weekly.note,
            ))
        work_date += datetime.timedelta(days=1)

    DateOpeningSchedule.objects.bulk_create(new_rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ('shift', '0004_add_is_draft_and_deadline'),
    ]

    operations = [
        migrations.RunPython(materialize_weekly_schedules, migrations.RunPython.noop),
        # SQLite はテーブル再構築を伴うため、フィールドより先に制約を落とす必要がある
        migrations.RemoveConstraint(
            model_name='weeklyopeningschedule',
            name='weekly_opening_schedule_unique_weekday',
        ),
        migrations.RemoveField(
            model_name='weeklyopeningschedule',
            name='group',
        ),
        migrations.RemoveField(
            model_name='weeklyopeningschedule',
            name='schedule_type',
        ),
        migrations.DeleteModel(
            name='HistoricalWeeklyOpeningSchedule',
        ),
        migrations.DeleteModel(
            name='WeeklyOpeningSchedule',
        ),
    ]
