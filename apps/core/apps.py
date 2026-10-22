import logging

from django.apps import AppConfig
from django.conf import settings

from apps.core.logging import log_event

logger = logging.getLogger(__name__)


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"

    def ready(self) -> None:
        self._warn_disabled_features()

    @staticmethod
    def _warn_disabled_features() -> None:
        """Optional features disable themselves cleanly; say so once at startup (§21.2)."""
        optional = {
            "telegram": settings.TELEGRAM_BOT_TOKEN,
            "google_signin": settings.GOOGLE_ENABLED,
            "inbound_email": settings.INBOUND_IMAP_USER,
            "sentry": settings.SENTRY_DSN,
        }
        for feature, value in optional.items():
            if not value:
                log_event(
                    logger,
                    logging.WARNING,
                    "feature_disabled",
                    "Optional feature disabled: its configuration is absent",
                    feature=feature,
                )
