"""What happens after a match is scored: an auto-kit (Phase 7) and an instant alert (Phase 8)."""

import logging

from apps.core.logging import log_event

from .models import Match

logger = logging.getLogger(__name__)


def after_score(match: Match) -> None:
    """Called after a match is scored. Must never raise: scoring has already been saved.

    The kit is started first, so the instant alert (which waits up to 90 seconds for it, spec
    §10.4) finds it in progress."""
    try:
        from apps.kits import services as kit_services

        kit_services.auto_generate_if_eligible(match)
    except Exception:
        log_event(
            logger, logging.ERROR, "auto_kit_failed", "Auto-kit could not be started",
            match_id=match.pk, exc_info=True,
        )  # fmt: skip
    try:
        from apps.notifications import services as notification_services

        notification_services.request_instant(match)
    except Exception:
        log_event(
            logger, logging.ERROR, "instant_alert_failed", "Instant alert could not be queued",
            match_id=match.pk, exc_info=True,
        )  # fmt: skip
