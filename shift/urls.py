from django.urls import path

from . import attendance_views, views

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
    path("schedule/<int:group_id>/time-slots/<int:time_slot_id>/delete/", views.time_slot_delete,
         name="time_slot_delete"),
    path("summary/", views.summary_index, name="summary_index"),
    path("summary/<int:group_id>/", views.summary, name="summary"),
    path("attendance/", attendance_views.attendance_index, name="attendance_index"),
    path("attendance/admin/", attendance_views.attendance_admin_index, name="attendance_admin_index"),
    path("attendance/<int:group_id>/", attendance_views.attendance, name="attendance"),
    path("attendance/<int:group_id>/clock/", attendance_views.attendance_clock, name="attendance_clock"),
    path("attendance/<int:group_id>/settings/", attendance_views.attendance_settings,
         name="attendance_settings"),
    path("attendance/<int:group_id>/summary/", attendance_views.attendance_summary,
         name="attendance_summary"),
]
