"""Send one real Telegram alert for a user's latest match, to check the connection."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import User
from apps.notifications import services

MESSAGES = {
    services.SENT: "Sent. Check Telegram.",
    "no_telegram": "No active Telegram link (or no token). Connect Telegram in Settings first.",
    "no_match": "That user has no matches yet, so there is nothing to show.",
    services.FAILED: "Telegram refused the message. See the log for the reason.",
    services.RETRY: "Telegram could not be reached. It will retry; see the log.",
    services.SKIPPED: "Skipped (the match was dismissed or applied).",
}


class Command(BaseCommand):
    help = "Send a sample alert (latest match, with buttons) to a user's linked Telegram."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("email", help="The user's login email")

    def handle(self, *args: Any, **options: Any) -> None:
        user = User.objects.filter(email__iexact=options["email"]).first()
        if user is None:
            raise CommandError("No user with that email.")
        outcome = services.send_test_alert(user)
        self.stdout.write(MESSAGES.get(outcome, f"Result: {outcome}"))
