"""Seams for later phases. Both are no-ops now and are filled in without touching scoring."""

from typing import Any


def after_score(match: Any) -> None:
    """Called after a match is scored.

    TODO(phase 7): enqueue kit generation when the score reaches `auto_kit_threshold`.
    TODO(phase 8): enqueue an instant notification at `instant_score_threshold` (it waits up to
    90 seconds for the kit)."""
