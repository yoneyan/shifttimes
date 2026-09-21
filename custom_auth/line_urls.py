from django.urls import path

from . import line_views

app_name = "custom_auth_line"
urlpatterns = [
    path("login/", line_views.line_login, name="login"),
    path("link/", line_views.line_link, name="link"),
    path("callback/", line_views.callback, name="callback"),
    path("unlink/", line_views.unlink, name="unlink"),
]
