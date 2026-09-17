from django.urls import path

from . import views

app_name = "shift"
urlpatterns = [
    path("", views.index, name="index"),
    path("calendar/<int:group_id>/", views.shift_calendar, name="shift_calendar"),
    path("confirm/<int:group_id>/", views.shift_confirm, name="shift_confirm"),
    path("entry-table/<int:group_id>/", views.entry_table, name="entry_table"),
    path("schedule/", views.schedule_index, name="schedule_index"),
    path("schedule/<int:group_id>/", views.schedule, name="schedule"),
    path("schedule/<int:group_id>/view/", views.schedule_member, name="schedule_member"),
    path("schedule/<int:group_id>/settings/", views.schedule_settings, name="schedule_settings"),
    path("schedule/<int:group_id>/deadlines/<int:deadline_id>/delete/", views.deadline_delete,
         name="deadline_delete"),
    path("summary/", views.summary_index, name="summary_index"),
    path("summary/<int:group_id>/", views.summary, name="summary"),
]
