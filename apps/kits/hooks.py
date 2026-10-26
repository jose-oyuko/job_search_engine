"""Seams for later phases."""

from typing import Any


def after_generate(kit: Any) -> None:
    """Called when a kit becomes ready.

    TODO(phase 8): send the "kit ready" notification (Telegram documents, email link) and release
    an instant match alert that was waiting for this kit (spec §10.4, §13.1)."""
