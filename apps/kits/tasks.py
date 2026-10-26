"""Huey tasks for kits (spec §18). Thin wrappers over `services`."""

from huey.contrib.djhuey import db_task

from apps.core.tasks import logged_task

from . import services


@db_task()
@logged_task
def generate_kit_task(kit_id: int) -> None:
    """Run the kit pipeline for one queued kit. Idempotent: a kit already picked up is skipped."""
    services.generate_kit(kit_id)
