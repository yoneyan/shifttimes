from django import forms
from django.contrib.auth.forms import AuthenticationForm, PasswordChangeForm, PasswordResetForm, SetPasswordForm

from custom_auth.models import Group, User, UserGroup


class GroupAddForm(forms.Form):
    # POST 値は文字列なので、"True" のときだけ True に変換する。
    # ChoiceField のままだと "False" も truthy な文字列として扱われ、同意しなくても通ってしまう
    agree = forms.TypedChoiceField(
        label="利用規約への同意", required=True,
        choices=(("False", "同意しない"), ("True", "同意する")),
        coerce=lambda value: value == "True",
        empty_value=False,
        initial="False",
    )
    name_jp = forms.CharField(label="グループ名", max_length=150, required=True)
    name = forms.CharField(label="グループ名(English)", max_length=150, required=True)
    postcode = forms.CharField(label="郵便番号", max_length=10, required=True)
    address_jp = forms.CharField(label="住所", max_length=250, required=True)
    address = forms.CharField(label="住所(English)", max_length=250, required=True)
    phone = forms.CharField(label="電話番号", max_length=30, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label

    def clean_agree(self):
        agree = self.cleaned_data.get("agree")
        if agree is not True:
            raise forms.ValidationError("規約に同意されていません")
        return agree

    def create_group(self, user_id):
        try:
            group = Group.objects.create_group(
                name=self.cleaned_data["name"],
                name_jp=self.cleaned_data["name_jp"],
                postcode=self.cleaned_data["postcode"],
                address=self.cleaned_data["address"],
                address_jp=self.cleaned_data["address_jp"],
                phone=self.cleaned_data["phone"],
            )
        except Exception as e:
            raise ValueError("グループの登録に失敗しました(名前が被っている可能性があります)") from e

        try:
            user = User.objects.get(id=user_id)
            user.allow_group_add = False
            user.save()
        except Exception as e:
            raise ValueError("ユーザ情報の変更に失敗しました") from e

        try:
            UserGroup.objects.create(user=user, group=group, is_admin=True)
        except Exception as e:
            raise ValueError("ユーザとグループの紐づけに失敗しました") from e


class StudentEditForm(forms.Form):
    membership_expired_at = forms.DateField(
        label="有効期限",
        required=True,
    )
    file = forms.FileField(
        label="学生証明",
        required=True,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label

    def create_group(self, user_id):
        try:
            group = Group.objects.create_group(
                name=self.cleaned_data["name"],
                postcode=self.cleaned_data["postcode"],
                address=self.cleaned_data["address"],
                address_en=self.cleaned_data["address_en"],
                email=self.cleaned_data["email"],
                phone=self.cleaned_data["phone"],
                country=self.cleaned_data["country"],
            )
        except Exception as e:
            raise ValueError("グループの登録に失敗しました(名前が被っている可能性があります。)") from e

        try:
            user = User.objects.get(id=user_id)
            user.group_add = False
            user.save()
        except Exception as e:
            raise ValueError("ユーザ情報の変更に失敗しました") from e

        try:
            UserGroup.objects.create(user=user, group=group, is_admin=True)
        except Exception as e:
            raise ValueError("ユーザとグループの紐づけに失敗しました") from e


class GroupForm(forms.ModelForm):
    class Meta:
        model = Group
        fields = ("postcode", "address_jp", "address", "phone", "country")
        labels = {
            "postcode": "郵便番号",
            "address_jp": "住所",
            "address": "住所(English)",
            "phone": "phone",
            "country": "居住国",
        }

    def __init__(self, *args, **kwargs):
        editable = kwargs.pop("editable", False)  # group_id を引数として受け取る
        super().__init__(*args, **kwargs)
        if editable:
            self.fields["postcode"].widget.attrs["disabled"] = True
            self.fields["address_jp"].widget.attrs["disabled"] = True
            self.fields["address"].widget.attrs["disabled"] = True
            self.fields["phone"].widget.attrs["disabled"] = True
            self.fields["country"].widget.attrs["disabled"] = True
        self.fields["postcode"].widget.attrs["class"] = "form-control p-postal-code"
        self.fields["address_jp"].widget.attrs["class"] = (
            "form-control p-region p-locality p-street-address p-extended-address"
        )
        self.fields["address"].widget.attrs["class"] = "form-control"
        self.fields["phone"].widget.attrs["class"] = "form-control"
        self.fields["country"].widget.attrs["class"] = "form-control p-country-name"

        for field in self.fields.values():
            field.widget.attrs["placeholder"] = field.label


class TwoAuthForm(forms.Form):
    title = forms.CharField(label="名前", max_length=100, required=True)
    code = forms.CharField(label="code", max_length=6, min_length=6, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class MyPasswordChangeForm(PasswordChangeForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["old_password"].widget.attrs["class"] = "form-control"
        self.fields["new_password1"].widget.attrs["class"] = "form-control"
        self.fields["new_password2"].widget.attrs["class"] = "form-control"
        self.fields["new_password1"].widget.attrs["placeholder"] = "半角英数字８文字以上"
        self.fields["new_password2"].widget.attrs["placeholder"] = "パスワード確認用"


class EmailChangeForm(forms.Form):
    email = forms.EmailField(label="メールアドレス", max_length=100, required=True)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label

    def clean_email(self):
        email = self.cleaned_data["email"]
        others = User.objects.filter(email=email)
        if self.user is not None:
            others = others.exclude(pk=self.user.pk)
        if others.exists():
            raise forms.ValidationError("このメールアドレスは既に使用されています")
        return email

    def save(self, user):
        try:
            User.objects.change_email(user=user, email=self.cleaned_data["email"])
        except Exception as e:
            raise ValueError("更新に失敗しました") from e


class ProfileEditForm(forms.Form):
    username = forms.CharField(label="名前(english)", max_length=100, required=True)
    username_jp = forms.CharField(label="名前(japanese)", max_length=100, required=True)
    display_name = forms.CharField(label="display_name", max_length=150, required=True)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label

    def _check_unique(self, field_name, value):
        others = User.objects.filter(**{field_name: value})
        if self.user is not None:
            others = others.exclude(pk=self.user.pk)
        if others.exists():
            raise forms.ValidationError("この名前は既に使用されています")

    def clean_username(self):
        username = User.normalize_username(self.cleaned_data["username"])
        self._check_unique("username", username)
        return username

    def clean_username_jp(self):
        username_jp = self.cleaned_data["username_jp"]
        self._check_unique("username_jp", username_jp)
        return username_jp

    def save(self, user):
        try:
            User.objects.update_user(
                user=user,
                username=self.cleaned_data["username"],
                username_jp=self.cleaned_data["username_jp"],
                display_name=self.cleaned_data["display_name"],
            )
        except Exception as e:
            raise ValueError("更新に失敗しました(名前が既に使用されている可能性があります)") from e


class LoginForm(AuthenticationForm):
    token = forms.CharField(label="token", max_length=150, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class OTPForm(forms.Form):
    token = forms.CharField(label="token", max_length=150, required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class AuthTOTP(forms.Form):
    token = forms.IntegerField(label="totp", required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class ForgetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class NewSetPasswordForm(SetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label


class SignUpForm(forms.Form):
    username = forms.CharField(label="ユーザネーム(english)", max_length=150, required=True)
    username_jp = forms.CharField(label="ユーザネーム(japanese)", max_length=150, required=True)
    display_name = forms.CharField(label="display_name", max_length=150, required=True)
    email = forms.EmailField(label="email", max_length=150, required=True)
    password1 = forms.CharField(label="password", widget=forms.PasswordInput(), min_length=8)
    password2 = forms.CharField(label="password(確認用)", widget=forms.PasswordInput(),
                                min_length=8)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
            field.widget.attrs["placeholder"] = field.label

    def clean(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 != password2:
            raise forms.ValidationError("パスワードが一致しません")

    def create_user(self):
        return User.objects.create_user(
            username=self.cleaned_data["username"],
            username_jp=self.cleaned_data["username_jp"],
            display_name=self.cleaned_data["display_name"],
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password1"],
        )
