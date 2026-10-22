from django.apps import AppConfig


class SourcesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sources"

    def ready(self) -> None:
        # Import the fetchers so their @register decorators run before the first lookup.
        from .registry import load_all

        load_all()
