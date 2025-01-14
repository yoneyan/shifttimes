import base64
from io import BytesIO

import pyotp
import qrcode
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetCompleteView, PasswordResetConfirmView, \
    PasswordResetView, PasswordResetDoneView
from django.shortcuts import redirect, render
from django.urls import reverse, reverse_lazy

from custom_auth.form import (
    EmailChangeForm,
    GroupAddForm,
    GroupForm,
    MyPasswordChangeForm,
    ProfileEditForm,
    TwoAuthForm, ForgetForm, NewSetPasswordForm,
)
from custom_auth.models import UserGroup
from custom_auth.models import TOTPDevice, UserActivateToken


@login_required
def index(request):
    context = {}
    return render(request, "notice/index.html", context)

