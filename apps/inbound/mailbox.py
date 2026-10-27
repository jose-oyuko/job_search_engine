"""The mailbox behind an interface (spec §9.1, §9.4).

`MailboxClient` is all the rest of the app knows. `ImapMailbox` talks to the platform's one
dedicated inbox with `imap-tools`; `FakeMailbox` serves saved `.eml` files so tests never touch a
network. Messages are parsed with the standard library into `MailMessage`, the same way for both.
"""

import email
import email.policy
import email.utils
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage, Message
from pathlib import Path
from typing import Any, Protocol

from django.conf import settings

logger = logging.getLogger(__name__)

PROCESSED = "jobpilot/processed"
UNROUTED = "jobpilot/unrouted"
# Where the address we were sent to may appear (spec §9.2).
RECIPIENT_HEADERS = ("To", "Delivered-To", "X-Forwarded-To", "X-Original-To", "Envelope-To", "Cc")


@dataclass
class MailMessage:
    uid: str
    message_id: str
    from_address: str
    subject: str
    received_at: datetime
    recipients: list[str]  # every address found in the routing headers, lower case
    text: str = ""
    html: str = ""
    headers: dict[str, str] = field(default_factory=dict)


class MailboxClient(Protocol):
    def fetch_unseen(self, limit: int) -> list[MailMessage]: ...

    def finish(self, uid: str, folder: str) -> None:
        """Mark the message read and move it to `folder`."""
        ...

    def close(self) -> None: ...


def _addresses(values: Iterable[str]) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses(list(values)) if a]


def _body(message: Message, kind: str) -> str:
    part = message.get_body(preferencelist=(kind,)) if isinstance(message, EmailMessage) else None
    if part is None:
        return ""
    try:
        return str(part.get_content())
    except (LookupError, UnicodeDecodeError, KeyError):
        return ""


def parse_message(raw: bytes | Message, uid: str = "") -> MailMessage:
    """Turn raw RFC 822 bytes (or a parsed message) into a `MailMessage`."""
    message: Message = (
        email.message_from_bytes(raw, policy=email.policy.default)
        if isinstance(raw, bytes)
        else raw
    )
    recipients: list[str] = []
    for name in RECIPIENT_HEADERS:
        recipients += _addresses(message.get_all(name, []))
    when = email.utils.parsedate_to_datetime(message["Date"]) if message["Date"] else None
    if when is None:
        when = datetime.now(UTC)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    sender = _addresses([str(message.get("From", ""))])
    return MailMessage(
        uid=uid,
        message_id=str(message.get("Message-ID", "")).strip().strip("<>")[:300],
        from_address=sender[0] if sender else "",
        subject=str(message.get("Subject", ""))[:300],
        received_at=when,
        recipients=recipients,
        text=_body(message, "plain"),
        html=_body(message, "html"),
        headers={k: str(v) for k, v in message.items() if k.lower() in ("from", "reply-to")},
    )


class FakeMailbox:
    """An in-memory mailbox. `finished` records where each message was filed."""

    def __init__(self, messages: Iterable[MailMessage] = ()) -> None:
        self.unseen = list(messages)
        self.finished: dict[str, str] = {}
        self.closed = False

    @classmethod
    def from_eml_files(cls, *paths: Path) -> "FakeMailbox":
        return cls(parse_message(p.read_bytes(), uid=str(i + 1)) for i, p in enumerate(paths))

    def add(self, message: MailMessage) -> None:
        self.unseen.append(message)

    def fetch_unseen(self, limit: int) -> list[MailMessage]:
        return [m for m in self.unseen if m.uid not in self.finished][:limit]

    def finish(self, uid: str, folder: str) -> None:
        self.finished[uid] = folder

    def close(self) -> None:
        self.closed = True


class ImapMailbox:
    """The real inbox over IMAP (Gmail app password). Opened per poll, closed afterwards."""

    def __init__(self, host: str, user: str, password: str) -> None:
        from imap_tools import MailBox

        self._box = MailBox(host).login(user, password)
        self._folders: set[str] = set()

    def _ensure_folder(self, folder: str) -> None:
        if folder in self._folders:
            return
        if not self._box.folder.exists(folder):
            self._box.folder.create(folder)
        self._folders.add(folder)

    def fetch_unseen(self, limit: int) -> list[MailMessage]:
        from imap_tools import AND

        found = self._box.fetch(AND(seen=False), limit=limit, mark_seen=False, bulk=False)
        return [parse_message(m.obj, uid=str(m.uid)) for m in found]

    def finish(self, uid: str, folder: str) -> None:
        from imap_tools import MailMessageFlags

        self._ensure_folder(folder)
        self._box.flag([uid], MailMessageFlags.SEEN, True)
        self._box.move([uid], folder)

    def close(self) -> None:
        try:
            self._box.logout()
        except Exception:  # a dead connection must not hide the real result
            logger.debug("IMAP logout failed", exc_info=True)


# The fake mailbox tests (and a dev with no mail account) share, so the task can find it.
_fake = FakeMailbox()


def fake_mailbox() -> FakeMailbox:
    return _fake


def mailbox_enabled() -> bool:
    return settings.INBOUND_MAILBOX == "fake" or bool(
        settings.INBOUND_IMAP_USER and settings.INBOUND_IMAP_PASSWORD
    )


def open_mailbox() -> Any:
    """The configured mailbox, or None when email ingestion is not set up."""
    if settings.INBOUND_MAILBOX == "fake":
        return _fake
    if settings.INBOUND_IMAP_USER and settings.INBOUND_IMAP_PASSWORD:
        return ImapMailbox(
            settings.INBOUND_IMAP_HOST, settings.INBOUND_IMAP_USER, settings.INBOUND_IMAP_PASSWORD
        )
    return None
