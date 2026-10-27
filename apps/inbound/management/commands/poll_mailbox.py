"""Read the dedicated inbox once, now (the same work the 5-minute task does)."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.inbound import services
from apps.inbound.mailbox import mailbox_enabled


class Command(BaseCommand):
    help = "Poll the job-alert inbox once and print what was found. Needs INBOUND_IMAP_*."

    def handle(self, *args: Any, **options: Any) -> None:
        if not mailbox_enabled():
            raise CommandError("INBOUND_IMAP_USER and INBOUND_IMAP_PASSWORD are not set.")
        counts = services.poll_mailbox()
        self.stdout.write(", ".join(f"{key}: {value}" for key, value in counts.items()))
