from django import forms

from shift.models import (
    AttendanceRecord,
    AttendanceSetting,
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
    TimeSlot,
    WEEKDAY_CHOICES,
)


class ShiftDeadlineForm(forms.ModelForm):
    """対象期間ごとのシフト提出期限"""

    class Meta:
        model = ShiftDeadline
        fields = ("period_start", "period_end", "deadline_date", "note")
        widgets = {
            "period_start": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "period_end": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "deadline_date": forms.DateInput(attrs={"type": "date", "class": "form-control"}),
            "note": forms.TextInput(attrs={"class": "form-control", "placeholder": "任意"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        period_start = cleaned_data.get("period_start")
        period_end = cleaned_data.get("period_end")
        if period_start and period_end and period_end < period_start:
            raise forms.ValidationError("対象期間の終了日は開始日以降にしてください。")
        return cleaned_data


class TimeSlotForm(forms.ModelForm):
    """グループごとの勤務時間（時間帯）"""

    class Meta:
        model = TimeSlot
        fields = ("name", "start_time", "end_time", "is_active")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "例: 早番、14:00-16:00"}),
            "start_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
            "end_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.group is None:
            return name

        time_slots = TimeSlot.objects.filter(group=self.group, name=name)
        if self.instance.pk:
            time_slots = time_slots.exclude(pk=self.instance.pk)
        if time_slots.exists():
            raise forms.ValidationError("同じ名前の勤務時間がすでにあります。")
        return name

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        if start_time and end_time and end_time <= start_time:
            raise forms.ValidationError("終了時刻は開始時刻より後にしてください。")
        return cleaned_data


class OpeningScheduleTypeForm(forms.ModelForm):
    """開講区分の文言とシフト入力可否"""

    class Meta:
        model = OpeningScheduleType
        fields = ("name", "blocks_shift_input", "is_active", "display_order")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "例: 休校、自習室"}),
            "blocks_shift_input": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "display_order": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, group=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.group = group

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.group is None:
            return name

        schedule_types = OpeningScheduleType.objects.filter(group=self.group, name=name)
        if self.instance.pk:
            schedule_types = schedule_types.exclude(pk=self.instance.pk)
        if schedule_types.exists():
            raise forms.ValidationError("同じ名前の開講区分がすでにあります。")
        return name


class AttendanceSettingForm(forms.ModelForm):
    """グループごとの勤怠管理機能の有効・無効"""

    class Meta:
        model = AttendanceSetting
        fields = ("is_enabled", "allow_manual_input", "allow_clock_button")
        widgets = {
            "is_enabled": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "allow_manual_input": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "allow_clock_button": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean(self):
        cleaned_data = super().clean()
        if not cleaned_data.get("is_enabled"):
            return cleaned_data
        if not cleaned_data.get("allow_manual_input") and not cleaned_data.get("allow_clock_button"):
            raise forms.ValidationError("手動入力と出退勤ボタンの少なくとも一方を許可してください。")
        return cleaned_data


class AttendanceRecordForm(forms.ModelForm):
    """1日ぶんの勤怠実績（手動入力・管理者による修正の共通フォーム）"""

    class Meta:
        model = AttendanceRecord
        fields = ("start_time", "end_time", "break_minutes", "note")
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
            "end_time": forms.TimeInput(attrs={"type": "time", "class": "form-control"}),
            "break_minutes": forms.NumberInput(attrs={"class": "form-control", "min": 0, "step": 5}),
            "note": forms.TextInput(attrs={"class": "form-control", "maxlength": 200}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["break_minutes"].required = False
        self.fields["note"].required = False

    def clean_break_minutes(self):
        return self.cleaned_data.get("break_minutes") or 0

    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get("start_time")
        end_time = cleaned_data.get("end_time")
        break_minutes = cleaned_data.get("break_minutes") or 0

        if end_time and not start_time:
            raise forms.ValidationError("退勤時刻を入力する場合は出勤時刻も入力してください。")

        if start_time and end_time:
            start = start_time.hour * 60 + start_time.minute
            end = end_time.hour * 60 + end_time.minute
            if end < start:  # 日をまたぐ勤務
                end += 24 * 60
            if break_minutes > end - start:
                raise forms.ValidationError("休憩時間が勤務時間を超えています。")

        return cleaned_data
