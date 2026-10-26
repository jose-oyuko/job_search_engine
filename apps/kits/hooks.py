"""What happens when a kit becomes ready."""

import logging
from typing import Any

from apps.core.logging import log_event

logger = logging.getLogger(__name__)


def after_generate(kit: Any) -> None:
    """Called when a kit is ready: send the "kit ready" messages waiting for it (Telegram
    documents, email link). Must never raise; the kit is already saved."""
    try:
        from apps.notifications import services as notification_services

        notification_services.on_kit_ready(kit)
    except Exception:
        log_event(
            logger, logging.ERROR, "kit_ready_notice_failed", "Kit ready notice failed",
            kit_id=kit.pk, exc_info=True,
        )  # fmt: skip
