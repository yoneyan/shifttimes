from django.apps import AppConfig


class Notice(AppConfig):
    name = "shift"
    verbose_name = "シフト"

    def ready(self):
        from . import signals  # noqa
