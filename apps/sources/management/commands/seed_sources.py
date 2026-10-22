"""Create the built-in job sources (ExpertWoka). Safe to run repeatedly; existing rows are kept.

uv run python manage.py seed_sources
"""

from typing import Any

from django.core.management.base import BaseCommand

from apps.sources.services import seed_sources


class Command(BaseCommand):
    help = "Create the built-in job sources if they do not exist yet."

    def handle(self, *args: Any, **options: Any) -> None:
        for name, created in seed_sources():
            self.stdout.write(f"{name}: {'created' if created else 'already exists'}")
