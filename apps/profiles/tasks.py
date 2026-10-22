"""Huey tasks for profiles. Thin wrappers: the work is in `services`."""

from huey.contrib.djhuey import db_task

from apps.core.tasks import logged_task

from . import services


@db_task()
@logged_task
def parse_cv_import(cv_import_id: int) -> None:
    """Parse one uploaded CV. Idempotent: a repeat run for the same import does nothing."""
    services.run_cv_parse(cv_import_id)
