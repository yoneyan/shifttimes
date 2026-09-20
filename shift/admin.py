from django.contrib import admin
from simple_history.admin import SimpleHistoryAdmin

from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
    TimeSlot,
)


@admin.register(TimeSlot)
class TimeSlotAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "name", "start_time", "end_time", "is_active")}),
    )
    list_display = ("id", "group", "name", "start_time", "end_time", "is_active", "updated_at")
    list_filter = ("group", "is_active")
    search_fields = ("name", "group__name")
    ordering = ("group", "start_time")


@admin.register(OpeningScheduleType)
class OpeningScheduleTypeAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "name", "blocks_shift_input", "is_active", "display_order")}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "name", "blocks_shift_input", "is_active", "display_order", "updated_at")
    list_filter = ("group", "blocks_shift_input", "is_active")
    search_fields = ("name", "group__name")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ShiftDeadline)
class ShiftDeadlineAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "period_start", "period_end", "deadline_date")}),
        ("info", {"fields": ("note",)}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "period_start", "period_end", "deadline_date", "updated_at")
    list_filter = ("group",)
    search_fields = ("group__name", "note")
    readonly_fields = ("created_at", "updated_at")


@admin.register(DateOpeningSchedule)
class DateOpeningScheduleAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "work_date", "schedule_type")}),
        ("info", {"fields": ("note",)}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "work_date", "schedule_type", "updated_at")
    list_filter = ("group", "schedule_type", "work_date")
    search_fields = ("group__name", "note")
    readonly_fields = ("created_at", "updated_at")


@admin.register(ShiftEntry)
class ShiftEntryAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "user", "work_date", "time_slot")}),
        ("info", {"fields": ("status", "note")}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "user", "work_date", "time_slot", "status", "updated_at")
    list_filter = ("group", "status", "work_date", "time_slot")
    search_fields = ("user__username", "group__name", "note")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AttendanceSetting)
class AttendanceSettingAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "is_enabled", "allow_manual_input", "allow_clock_button")}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "is_enabled", "allow_manual_input", "allow_clock_button", "updated_at")
    list_filter = ("is_enabled", "allow_manual_input", "allow_clock_button")
    search_fields = ("group__name", "group__name_jp")
    readonly_fields = ("created_at", "updated_at")


@admin.register(AttendanceRecord)
class AttendanceRecordAdmin(SimpleHistoryAdmin):
    fieldsets = (
        (None, {"fields": ("group", "user", "work_date")}),
        ("info", {"fields": ("start_time", "end_time", "break_minutes", "source", "note")}),
        ("audit", {"fields": ("created_at", "updated_at")}),
    )
    list_display = ("id", "group", "user", "work_date", "start_time", "end_time", "break_minutes",
                    "source", "updated_at")
    list_filter = ("group", "source", "work_date")
    search_fields = ("user__username", "group__name", "note")
    readonly_fields = ("created_at", "updated_at")
