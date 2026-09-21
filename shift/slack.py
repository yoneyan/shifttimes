"""グループ管理者が設定する Slack 通知（Incoming Webhook）

運営向けの DB 変更通知（``shifttimes.notify``）とは別系統で、送信先はグループごとに
管理者が設定した Webhook URL になる。送信に失敗しても画面の操作は止めない方針なので、
呼び出し側には成功可否だけを返し、例外は外に出さない。
"""
import logging

from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from slack_sdk import WebhookClient

from custom_auth.models import UserGroup
from shift.models import ShiftEntry, SlackNotificationSetting


logger = logging.getLogger(__name__)

# Webhook の応答待ちで画面の操作が止まらないよう、短めに区切る
SEND_TIMEOUT_SECONDS = 5

COLOR_INFO = "#0d6efd"
COLOR_WARN = "#ffc107"
COLOR_GOOD = "#198754"


def get_setting(group):
    """グループの Slack 通知設定を取得する（未作成なら既定値で作成する）"""
    setting, _created = SlackNotificationSetting.objects.get_or_create(group=group)
    return setting


def _absolute_url(path):
    return "%s%s" % (settings.SITE_URL.rstrip("/"), path)


def shift_entry_url(group):
    """メンバーがシフト希望を入力する画面の絶対 URL"""
    return _absolute_url(reverse("shift:entry_table", args=[group.id]))


def summary_url(group):
    """管理者が提出状況を見る画面の絶対 URL"""
    return _absolute_url(reverse("shift:summary", args=[group.id]))


def send(setting, title, text, color=COLOR_INFO, mention=True):
    """Slack へ1件送信する。送信できたかどうかを返す（失敗理由はログに残す）"""
    if setting is None or not setting.is_ready:
        return False, "Slack通知が有効になっていません。"

    body = text
    if mention and setting.mention:
        body = "%s\n%s" % (setting.mention, text)

    try:
        response = WebhookClient(setting.webhook_url, timeout=SEND_TIMEOUT_SECONDS).send(
            text=title,
            attachments=[{"color": color, "title": title, "text": body}],
        )
    except Exception as error:  # noqa: BLE001 - 通知の失敗で画面の操作を止めない
        logger.warning("Slack通知の送信に失敗しました group=%s: %s", setting.group_id, error)
        return False, "Slackへの送信に失敗しました: %s" % error

    if response.status_code != 200:
        logger.warning(
            "Slack通知が拒否されました group=%s status=%s body=%s",
            setting.group_id, response.status_code, response.body,
        )
        return False, "Slackへの送信に失敗しました（%s: %s）" % (response.status_code, response.body)

    return True, ""


def _member_label(user):
    return user.get_display_name()


def unsubmitted_members(group, period_start, period_end):
    """対象期間に確定済みのシフト希望が1件もないメンバー"""
    submitted_ids = set(
        ShiftEntry.objects.filter(
            group=group,
            work_date__gte=period_start,
            work_date__lte=period_end,
            is_draft=False,
        ).values_list("user_id", flat=True)
    )
    members = [
        membership.user
        for membership in UserGroup.objects.filter(group=group, user__is_active=True).select_related("user")
    ]
    return sorted(
        (user for user in members if user.id not in submitted_ids),
        key=lambda user: (_member_label(user), user.username),
    )


def _period_line(period_start, period_end, deadline_date=None):
    line = "対象期間: %s 〜 %s" % (period_start.strftime("%Y/%m/%d"), period_end.strftime("%Y/%m/%d"))
    if deadline_date:
        line += "\n提出期限: %s" % deadline_date.strftime("%Y/%m/%d")
    return line


def _unsubmitted_line(members):
    if not members:
        return "未提出: なし（全員提出済みです）"
    names = "、".join(_member_label(user) for user in members)
    return "未提出（%d名）: %s" % (len(members), names)


def notify_shift_request(group, period_start, period_end, deadline_date=None,
                         message="", include_unsubmitted=True, setting=None):
    """「シフトを入力してください」を手動で送る"""
    setting = setting or get_setting(group)

    lines = [_period_line(period_start, period_end, deadline_date)]
    if message:
        lines.append(message)
    if include_unsubmitted:
        lines.append(_unsubmitted_line(unsubmitted_members(group, period_start, period_end)))
    lines.append("入力はこちら: %s" % shift_entry_url(group))

    return send(setting, "[%s] シフト希望の入力をお願いします" % group.name_jp,
                "\n".join(lines), color=COLOR_INFO)


def notify_deadline_reminder(group, deadline, setting=None):
    """提出期限が近いことを自動で知らせる"""
    setting = setting or get_setting(group)
    if not setting.notify_deadline_reminder:
        return False, "リマインド通知が無効です。"

    members = unsubmitted_members(group, deadline.period_start, deadline.period_end)
    days_left = (deadline.deadline_date - timezone.now().date()).days
    lines = [
        _period_line(deadline.period_start, deadline.period_end, deadline.deadline_date),
        "本日が提出期限です。" if days_left <= 0 else "提出期限まであと%d日です。" % days_left,
        _unsubmitted_line(members),
        "入力はこちら: %s" % shift_entry_url(group),
    ]

    return send(setting, "[%s] シフト希望の提出期限が近づいています" % group.name_jp,
                "\n".join(lines), color=COLOR_WARN)


def notify_shift_confirmed(group, user, count, date_from, date_to, setting=None):
    """メンバーがシフト希望を確定したことを知らせる"""
    setting = setting or get_setting(group)
    if not setting.notify_shift_confirmed:
        return False, "シフト確定の通知が無効です。"

    text = "%s さんがシフト希望を確定しました。\n%s\n件数: %d件\n提出状況: %s" % (
        _member_label(user),
        _period_line(date_from, date_to),
        count,
        summary_url(group),
    )
    return send(setting, "[%s] シフト希望が提出されました" % group.name_jp, text,
                color=COLOR_GOOD, mention=False)


def notify_deadline_changed(group, deadline, created, setting=None):
    """提出期限の登録・変更を知らせる"""
    setting = setting or get_setting(group)
    if not setting.notify_schedule_changed:
        return False, "スケジュール変更の通知が無効です。"

    lines = [_period_line(deadline.period_start, deadline.period_end, deadline.deadline_date)]
    if deadline.note:
        lines.append("メモ: %s" % deadline.note)
    lines.append("入力はこちら: %s" % shift_entry_url(group))

    title = "[%s] シフト提出期限が%sされました" % (group.name_jp, "設定" if created else "変更")
    return send(setting, title, "\n".join(lines), color=COLOR_INFO)


def notify_schedule_changed(group, dates, schedule_type_name, setting=None):
    """開講スケジュールの変更を知らせる"""
    setting = setting or get_setting(group)
    if not setting.notify_schedule_changed:
        return False, "スケジュール変更の通知が無効です。"

    dates = sorted(dates)
    if not dates:
        return False, "対象の日付がありません。"

    if len(dates) <= 10:
        date_label = "、".join("%d/%d" % (d.month, d.day) for d in dates)
    else:
        date_label = "%s 〜 %s の%d日" % (
            dates[0].strftime("%Y/%m/%d"), dates[-1].strftime("%Y/%m/%d"), len(dates),
        )

    text = "対象日: %s\n開講区分: %s\nカレンダー: %s" % (
        date_label,
        schedule_type_name or "未設定（曜日の基本スケジュール）に戻しました",
        _absolute_url(reverse("shift:schedule_member", args=[group.id])),
    )
    return send(setting, "[%s] 開講スケジュールが変更されました" % group.name_jp, text,
                color=COLOR_INFO, mention=False)


def notify_test(group, setting=None):
    """設定画面からのテスト送信"""
    setting = setting or get_setting(group)
    return send(
        setting,
        "[%s] Slack通知のテスト" % group.name_jp,
        "この通知が見えていれば、Slack連携の設定は正しく行えています。",
        color=COLOR_GOOD,
        mention=False,
    )
