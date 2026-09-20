from django import forms

from notice.models import Notice


DATETIME_LOCAL_FORMAT = "%Y-%m-%dT%H:%M"


class NoticeForm(forms.ModelForm):
    """運営が掲示する通知（お知らせ）"""

    class Meta:
        model = Notice
        fields = (
            "type1", "title", "body", "start_at", "end_at",
            "is_active", "is_important", "is_fail", "is_info",
        )
        widgets = {
            "type1": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control", "maxlength": 250}),
            "body": forms.Textarea(attrs={"class": "form-control", "rows": 6}),
            "start_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "form-control"}, format=DATETIME_LOCAL_FORMAT,
            ),
            "end_at": forms.DateTimeInput(
                attrs={"type": "datetime-local", "class": "form-control"}, format=DATETIME_LOCAL_FORMAT,
            ),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_important": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_fail": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "is_info": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["body"].required = False
        self.fields["end_at"].required = False

    def clean(self):
        cleaned_data = super().clean()
        start_at = cleaned_data.get("start_at")
        end_at = cleaned_data.get("end_at")
        if start_at and end_at and end_at <= start_at:
            raise forms.ValidationError("通知終了日は通知開始日より後にしてください。")
        return cleaned_data
