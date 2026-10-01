from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from shifttimes.models import MediumTextField


WEEKDAY_CHOICES = (
    (0, "月"),
    (1, "火"),
    (2, "水"),
    (3, "木"),
    (4, "金"),
    (5, "土"),
    (6, "日"),
)


class TimeSlot(models.Model):
    """グループごとに管理者が作成する勤務時間（時間帯）マスタ"""

    class Meta:
        ordering = ("start_time",)
        constraints = [
            models.CheckConstraint(
                check=models.Q(start_time__lt=models.F("end_time")),
                name="time_slot_start_before_end",
            ),
            models.UniqueConstraint(
                fields=("group", "name"),
                name="time_slot_unique_name",
            ),
        ]
        indexes = [
            models.Index(fields=("group", "start_time")),
        ]
        verbose_name = "勤務時間"
        verbose_name_plural = "勤務時間"

    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE, related_name="time_slots",
                              verbose_name="グループ")
    name = models.CharField("時間帯名", max_length=100)
    start_time = models.TimeField("開始時刻")
    end_time = models.TimeField("終了時刻")
    is_active = models.BooleanField("有効", default=True)
    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        return "%s (%s〜%s)" % (self.name, self.start_time.strftime("%H:%M"), self.end_time.strftime("%H:%M"))

    @property
    def display_label(self):
        """時間帯名が時刻そのものの場合に時刻を二重表示しない表示用ラベル"""
        start = self.start_time.strftime("%H:%M")
        end = self.end_time.strftime("%H:%M")
        if start in self.name and end in self.name:
            return self.name
        return "%s %s〜%s" % (self.name, start, end)


SCHEDULE_TYPE_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#[0-9a-fA-F]{6}$",
    message="表示色は #rrggbb の形式で指定してください。",
)


class OpeningScheduleType(models.Model):
    """グループごとに変更できる開講区分"""

    class Meta:
        ordering = ("display_order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("group", "name"),
                name="opening_schedule_type_unique_name",
            ),
        ]
        verbose_name = "開講区分"
        verbose_name_plural = "開講区分"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE, related_name="opening_schedule_types",
                              verbose_name="グループ")
    name = models.CharField("区分名", max_length=100)
    color = models.CharField("表示色", max_length=7, default="#0d6efd",
                             validators=[SCHEDULE_TYPE_COLOR_VALIDATOR])
    blocks_shift_input = models.BooleanField("シフト入力不可", default=False)
    is_active = models.BooleanField("有効", default=True)
    display_order = models.PositiveSmallIntegerField("表示順", default=100)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s" % (self.group, self.name)


class DateOpeningSchedule(models.Model):
    """日付ごとの開講スケジュール上書き"""

    class Meta:
        ordering = ("work_date",)
        constraints = [
            models.UniqueConstraint(
                fields=("group", "work_date"),
                name="date_opening_schedule_unique_date",
            ),
        ]
        indexes = [
            models.Index(fields=("group", "work_date")),
        ]
        verbose_name = "日付別開講スケジュール"
        verbose_name_plural = "日付別開講スケジュール"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE, related_name="date_opening_schedules",
                              verbose_name="グループ")
    work_date = models.DateField("日付", db_index=True)
    schedule_type = models.ForeignKey(OpeningScheduleType, on_delete=models.PROTECT,
                                      related_name="date_opening_schedules", verbose_name="開講区分")
    note = MediumTextField("メモ", default="", blank=True)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s [%s]" % (self.group, self.work_date, self.schedule_type.name)


class ShiftEntry(models.Model):
    """ユーザーの時間帯別シフト希望"""

    class Meta:
        ordering = ("work_date", "time_slot__start_time")
        constraints = [
            models.UniqueConstraint(
                fields=("group", "user", "work_date", "time_slot"),
                name="shift_entry_unique_time_slot",
            ),
        ]
        indexes = [
            models.Index(fields=("group", "work_date")),
            models.Index(fields=("user", "work_date")),
        ]
        verbose_name = "シフト希望"
        verbose_name_plural = "シフト希望"

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    MAYBE = "maybe"
    STATUS_CHOICES = (
        (AVAILABLE, "勤務可能"),
        (UNAVAILABLE, "勤務不可"),
        (MAYBE, "要相談"),
    )

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE, related_name="shift_entries",
                              verbose_name="グループ")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shift_entries",
                             verbose_name="ユーザー")
    work_date = models.DateField("勤務日", db_index=True)
    time_slot = models.ForeignKey(TimeSlot, on_delete=models.PROTECT, related_name="shift_entries",
                                  verbose_name="時間帯")
    status = models.CharField("ステータス", max_length=20, choices=STATUS_CHOICES, default=AVAILABLE)
    note = MediumTextField("メモ", default="", blank=True)
    is_draft = models.BooleanField("下書き", default=False, db_index=True)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s %s [%s]%s" % (
            self.id, self.work_date, self.time_slot, self.get_status_display(),
            " [下書き]" if self.is_draft else "",
        )


class ShiftDeadline(models.Model):
    """シフト希望提出の期限日（グループ・期間ごとに管理者が設定）"""

    class Meta:
        ordering = ("period_start",)
        constraints = [
            models.UniqueConstraint(
                fields=("group", "period_start"),
                name="shift_deadline_unique_period",
            ),
        ]
        verbose_name = "シフト提出期限"
        verbose_name_plural = "シフト提出期限"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey(
        "custom_auth.Group", on_delete=models.CASCADE,
        related_name="shift_deadlines", verbose_name="グループ",
    )
    period_start = models.DateField("対象期間 開始日")
    period_end = models.DateField("対象期間 終了日")
    deadline_date = models.DateField("提出期限日")
    note = MediumTextField("メモ", default="", blank=True)
    # Slack のリマインドを二重に送らないための記録。期限を変更すると送信し直せるように null に戻す
    reminder_sent_at = models.DateTimeField("リマインド送信日時", blank=True, null=True)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s〜%s (期限 %s)" % (
            self.group, self.period_start, self.period_end, self.deadline_date
        )


class AttendanceSetting(models.Model):
    """グループごとの勤怠管理機能の有効・無効設定"""

    class Meta:
        verbose_name = "勤怠設定"
        verbose_name_plural = "勤怠設定"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.OneToOneField("custom_auth.Group", on_delete=models.CASCADE,
                                 related_name="attendance_setting", verbose_name="グループ")
    is_enabled = models.BooleanField("勤怠管理を利用する", default=False)
    allow_manual_input = models.BooleanField("手動入力を許可", default=True)
    allow_clock_button = models.BooleanField("出退勤ボタンを許可", default=True)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s" % (self.group, "有効" if self.is_enabled else "無効")


class WorkType(models.Model):
    """グループごとの仕事内容（勤怠の項目）と時給"""

    class Meta:
        ordering = ("display_order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("group", "name"),
                name="work_type_unique_name",
            ),
        ]
        verbose_name = "仕事内容"
        verbose_name_plural = "仕事内容"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE, related_name="work_types",
                              verbose_name="グループ")
    name = models.CharField("仕事内容", max_length=100)
    hourly_rate = models.PositiveIntegerField("時給（円）", default=0)
    is_active = models.BooleanField("有効", default=True)
    display_order = models.PositiveSmallIntegerField("表示順", default=100)
    history = HistoricalRecords()

    def __str__(self):
        return f"{self.group}: {self.name}"

    @property
    def hourly_rate_display(self):
        return format_yen(self.hourly_rate)

    @property
    def display_label(self):
        """選択肢に出す「仕事内容（時給）」の表記"""
        return f"{self.name}（{self.hourly_rate_display}/時）"


class AttendanceRecord(models.Model):
    """ユーザーの勤怠実績（出勤・退勤・休憩）。同じ日に複数件登録できる"""

    class Meta:
        ordering = ("work_date", "start_time", "id")
        indexes = [
            models.Index(fields=("group", "work_date")),
            models.Index(fields=("user", "work_date")),
        ]
        verbose_name = "勤怠実績"
        verbose_name_plural = "勤怠実績"

    MANUAL = "manual"
    CLOCK = "clock"
    SOURCE_CHOICES = (
        (MANUAL, "手動入力"),
        (CLOCK, "打刻"),
    )

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.ForeignKey("custom_auth.Group", on_delete=models.CASCADE,
                              related_name="attendance_records", verbose_name="グループ")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="attendance_records", verbose_name="ユーザー")
    work_date = models.DateField("勤務日", db_index=True)
    work_type = models.ForeignKey(WorkType, on_delete=models.PROTECT, null=True, blank=True,
                                  related_name="attendance_records", verbose_name="仕事内容")
    # 仕事内容の時給を登録時点で写しておく。時給を変更しても登録済みの勤怠の金額は変わらない
    hourly_rate = models.PositiveIntegerField("時給（円）", null=True, blank=True)
    start_time = models.TimeField("出勤時刻", null=True, blank=True)
    end_time = models.TimeField("退勤時刻", null=True, blank=True)
    break_minutes = models.PositiveSmallIntegerField("休憩時間（分）", default=0)
    note = MediumTextField("メモ", default="", blank=True)
    source = models.CharField("入力方法", max_length=20, choices=SOURCE_CHOICES, default=MANUAL)
    history = HistoricalRecords()

    def __str__(self):
        return "%s: %s %s %s〜%s" % (
            self.id, self.user, self.work_date,
            self.start_time.strftime("%H:%M") if self.start_time else "--:--",
            self.end_time.strftime("%H:%M") if self.end_time else "--:--",
        )

    def apply_work_type(self, work_type):
        """仕事内容を設定し、その時点の時給を写しておく"""
        self.work_type = work_type
        self.hourly_rate = work_type.hourly_rate if work_type else None

    @property
    def is_working(self):
        """出勤済みで、まだ退勤していない状態"""
        return bool(self.start_time and not self.end_time)

    @property
    def is_overnight(self):
        """退勤時刻が出勤時刻より前（日をまたいだ勤務）かどうか"""
        return bool(self.start_time and self.end_time and self.end_time < self.start_time)

    @property
    def minute_range(self):
        """勤務日の 0:00 を起点にした (出勤, 退勤) の分。出勤がなければ None、退勤がなければ退勤は None"""
        return minute_range(self.start_time, self.end_time)

    @property
    def worked_minutes(self):
        """休憩を除いた実働時間（分）。出勤・退勤が揃っていない場合は None"""
        span = self.minute_range
        if span is None or span[1] is None:
            return None
        return max(span[1] - span[0] - self.break_minutes, 0)

    @property
    def worked_time_display(self):
        """実働時間を H:MM 形式で返す"""
        return minutes_to_hhmm(self.worked_minutes)

    @property
    def amount(self):
        """実働時間 × 時給の金額（円）。1 円未満は四捨五入。計算できない場合は None"""
        return calculate_amount(self.worked_minutes, self.hourly_rate)

    @property
    def amount_display(self):
        return format_yen(self.amount)


def minute_range(start_time, end_time):
    """出勤・退勤時刻を勤務日の 0:00 起点の分に直す（日をまたぐ退勤は 24:00 以降として扱う）"""
    if not start_time:
        return None
    start = start_time.hour * 60 + start_time.minute
    if not end_time:
        return (start, None)
    end = end_time.hour * 60 + end_time.minute
    if end < start:  # 日をまたぐ勤務は翌日の退勤として扱う
        end += 24 * 60
    return (start, end)


def find_overlapping_record(records, start_time, end_time):
    """同じ日の勤怠のうち、指定した時間帯と重なるものを返す（なければ None）

    退勤が未記録の勤怠は出勤時刻の 1 分間だけを占めるものとして扱う。
    同じ時刻に出勤した勤怠を二重に登録しないためで、終わりの見えない勤務と
    後から登録する勤怠を重複扱いにはしない。
    """
    span = minute_range(start_time, end_time)
    if span is None:
        return None
    start, end = span[0], span[1] if span[1] is not None else span[0] + 1
    for record in records:
        other = record.minute_range
        if other is None:
            continue
        other_start, other_end = other[0], other[1] if other[1] is not None else other[0] + 1
        if start < other_end and other_start < end:
            return record
    return None


def calculate_amount(minutes, hourly_rate):
    """分と時給から金額（円）を求める。1 円未満は四捨五入"""
    if minutes is None or hourly_rate is None:
        return None
    return (minutes * hourly_rate + 30) // 60


def format_yen(amount):
    """金額を「1,234円」の形にする（None は空文字）"""
    if amount is None:
        return ""
    return f"{amount:,}円"


def minutes_to_hhmm(minutes):
    """分を H:MM 表記に変換する（None は空文字）"""
    if minutes is None:
        return ""
    return "%d:%02d" % (minutes // 60, minutes % 60)


class SlackNotificationSetting(models.Model):
    """グループごとの Slack 通知設定（Incoming Webhook）

    運営向けの通知（``shifttimes.notify``）とは別物で、こちらはグループ管理者が
    自分たちのワークスペースに向けて設定する。
    """

    class Meta:
        verbose_name = "Slack通知設定"
        verbose_name_plural = "Slack通知設定"

    created_at = models.DateTimeField("作成日", default=timezone.now, db_index=True)
    updated_at = models.DateTimeField("更新日", auto_now=True)
    group = models.OneToOneField("custom_auth.Group", on_delete=models.CASCADE,
                                 related_name="slack_setting", verbose_name="グループ")
    is_enabled = models.BooleanField("Slack通知を利用する", default=False)
    webhook_url = models.URLField("Incoming Webhook URL", max_length=500, default="", blank=True)
    mention = models.CharField("メンション", max_length=100, default="", blank=True,
                               help_text="例: <!here>、<!channel>、<@U01ABCDEFG>")
    notify_deadline_reminder = models.BooleanField("提出期限のリマインド", default=True)
    reminder_days_before = models.PositiveSmallIntegerField("リマインドする日数（期限の何日前）", default=3)
    notify_shift_confirmed = models.BooleanField("メンバーのシフト確定", default=False)
    notify_schedule_changed = models.BooleanField("提出期限・開講スケジュールの変更", default=True)
    # Webhook URL は秘密情報なので履歴には残さない
    history = HistoricalRecords(excluded_fields=["webhook_url"])

    def __str__(self):
        return "%s: %s" % (self.group, "有効" if self.is_enabled else "無効")

    @property
    def is_ready(self):
        """実際に送信できる状態か（有効かつ Webhook URL が設定済み）"""
        return bool(self.is_enabled and self.webhook_url)

    @property
    def masked_webhook_url(self):
        """画面表示用に伏せ字にした Webhook URL"""
        if not self.webhook_url:
            return ""
        return "%s…%s" % (self.webhook_url[:34], self.webhook_url[-4:])
