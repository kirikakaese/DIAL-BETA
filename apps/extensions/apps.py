from django.apps import AppConfig


class ExtensionsConfig(AppConfig):
    name = "apps.extensions"
    verbose_name = "Extensions"

    def ready(self):
        from . import audio, signals  # noqa: F401  - audio: registers the Celery task
