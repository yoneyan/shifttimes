from django import forms

from shift.models import (
    DateOpeningSchedule,
    OpeningScheduleType,
    ShiftDeadline,
    ShiftEntry,
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
