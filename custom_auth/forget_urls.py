from django.urls import path

from . import views

app_name = "custom_auth_forget"
urlpatterns = [
    path("", views.list_groups, name="index"),
    path("done/", views.list_group, name="list"),
    path("confirm/<uidb64>/<token>/", views.add_group, name="add"),
    path("complete/", views.PasswordResetComplete.as_view(), name="edit"),
]
