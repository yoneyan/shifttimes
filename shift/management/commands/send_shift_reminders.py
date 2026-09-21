"""シフト希望の提出期限が近いグループへ Slack でリマインドを送る

cron などから1日1回呼ぶことを想定している。1つの提出期限につき1回だけ送り、
送信済みかどうかは ``ShiftDeadline.reminder_sent_at`` で判定する。

    uv run python manage.py send_shift_reminders
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from shift import slack
from shift.models import ShiftDeadline, SlackNotificationSetting


ACTIVE_GROUP_STATUS = 1


class Command(BaseCommand):
    help = "提出期限が近いシフトのリマインドを Slack へ送る"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="送信せずに対象だけを表示する",
        )
        parser.add_argument(
            "--group-id", type=int, default=None,
            help="特定のグループだけを対象にする",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        today = timezone.now().date()

        settings_by_group = {
            setting.group_id: setting
            for setting in SlackNotificationSetting.objects.filter(
                is_enabled=True, notify_deadline_reminder=True,
            ).exclude(webhook_url="").select_related("group")
        }
        if options["group_id"] is not None:
            settings_by_group = {
                group_id: setting
                for group_id, setting in settings_by_group.items()
                if group_id == options["group_id"]
            }
        if not settings_by_group:
            self.stdout.write("リマインド対象のグループがありません。")
            return

        deadlines = (
            ShiftDeadline.objects.filter(
                group_id__in=settings_by_group.keys(),
                group__status=ACTIVE_GROUP_STATUS,
                deadline_date__gte=today,
                reminder_sent_at__isnull=True,
            )
            .select_related("group")
            .order_by("group__name", "deadline_date")
        )

        sent_count = 0
        for deadline in deadlines:
            setting = settings_by_group[deadline.group_id]
            days_left = (deadline.deadline_date - today).days
            if days_left > setting.reminder_days_before:
                continue

            label = "%s（期限 %s / あと%d日）" % (
                deadline.group.name_jp, deadline.deadline_date, days_left,
            )
            if dry_run:
                self.stdout.write("[dry-run] %s" % label)
                continue

            ok, error = slack.notify_deadline_reminder(deadline.group, deadline, setting=setting)
            if ok:
                deadline.reminder_sent_at = timezone.now()
                deadline.save(update_fields=["reminder_sent_at", "updated_at"])
                sent_count += 1
                self.stdout.write(self.style.SUCCESS("送信しました: %s" % label))
            else:
                self.stderr.write("送信できませんでした: %s (%s)" % (label, error))

        if dry_run:
            self.stdout.write("dry-run のため送信していません。")
        else:
            self.stdout.write("%d件のリマインドを送信しました。" % sent_count)
