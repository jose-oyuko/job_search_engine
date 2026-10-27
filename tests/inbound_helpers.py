"""Shared setup for email-ingestion tests: saved `.eml` files addressed to a real user."""

from pathlib import Path
from typing import Any

from apps.inbound import services
from apps.inbound.mailbox import FakeMailbox, MailMessage, parse_message

FIXTURES = Path(__file__).parent / "fixtures" / "emails"
PLACEHOLDER = "u-xxxxxx@alerts.example.test"


def load_eml(
    name: str, user: Any = None, *, uid: str = "1", address: str | None = None
) -> MailMessage:
    """A fixture email, with its placeholder recipient replaced by `user`'s forwarding address."""
    raw = (FIXTURES / name).read_text(encoding="utf-8")
    if user is not None:
        raw = raw.replace(PLACEHOLDER, services.ensure_address(user).address)
    elif address is not None:
        raw = raw.replace(PLACEHOLDER, address)
    return parse_message(raw.encode(), uid=uid)


def mailbox_with(*messages: MailMessage) -> FakeMailbox:
    """Put messages into the shared fake mailbox that the `poll_mailbox` task reads."""
    from apps.inbound.mailbox import fake_mailbox

    box = fake_mailbox()
    box.unseen.clear()
    box.finished.clear()
    for message in messages:
        box.add(message)
    return box
