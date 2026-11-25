from django.urls import path

from . import views

app_name = "custom_auth_forget"
urlpatterns = [
    path("", views.PasswordReset.as_view(), name="index"),
    path("done/", views.PasswordResetDone.as_view(), name="done"),
    path("confirm/<uidb64>/<token>/", views.PasswordResetConfirm.as_view(), name="confirm"),
    path("complete/", views.PasswordResetComplete.as_view(), name="complete"),
]
