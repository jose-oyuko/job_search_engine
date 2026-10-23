"""Huey tasks for matching (spec §18). Thin wrappers over `services`."""

from huey.contrib.djhuey import db_task

from apps.core.tasks import logged_task

from . import services


@db_task()
@logged_task
def match_new_job_task(job_id: int) -> None:
    """Rule-filter a new job against every matchable user and queue scoring for the matches."""
    services.match_new_job(job_id)


@db_task()
@logged_task
def score_match_task(match_id: int) -> None:
    """LLM-score one match (within the user's allowance). Idempotent."""
    services.score_match(match_id)


@db_task()
@logged_task
def rematch_user_task(user_id: int) -> None:
    """Re-run the rule filter for one user after a profile or track change."""
    services.rematch_user(user_id)


@db_task()
@logged_task
def search_now_task(user_id: int) -> None:
    """The work behind the dashboard's "Search now" button."""
    services.run_search(user_id)
