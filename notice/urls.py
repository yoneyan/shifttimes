from django.urls import path

from . import views

app_name = "notice"
urlpatterns = [
    path("", views.index, name="index"),
    path("manage/", views.manage, name="manage"),
    path("manage/<int:notice_id>/delete/", views.notice_delete, name="notice_delete"),
]
