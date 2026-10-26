"""Seams for later phases. `after_score` starts auto-kits (Phase 7); Phase 8 adds notifications."""

import logging

from apps.core.logging import log_event

from .models import Match

logger = logging.getLogger(__name__)


def after_score(match: Match) -> None:
    """Called after a match is scored. Must never raise: scoring has already been saved.

    TODO(phase 8): enqueue an instant notification at `instant_score_threshold` (it waits up to
    90 seconds for the kit)."""
    try:
        from apps.kits import services as kit_services

        kit_services.auto_generate_if_eligible(match)
    except Exception:
        log_event(
            logger, logging.ERROR, "auto_kit_failed", "Auto-kit could not be started",
            match_id=match.pk, exc_info=True,
        )  # fmt: skip
