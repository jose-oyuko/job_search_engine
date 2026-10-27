"""Alert-email parsers and their registry (spec §9.2). A parser turns one email into jobs, using
only what is in the email; it never fetches the site the alert came from (LinkedIn)."""

from dataclasses import dataclass, field
from typing import Protocol

from apps.accounts.models import User
from apps.inbound.mailbox import MailMessage
from apps.jobs.models import Job

MAX_JOBS_PER_EMAIL = 15


@dataclass
class ParsedJob:
    title: str
    company: str
    url: str
    external_id: str  # stable and unique per posting, e.g. "linkedin:3912345678"
    locations: list[str] = field(default_factory=list)
    work_type: str = ""
    pay_text: str = ""
    description_text: str = ""


@dataclass
class ParseResult:
    jobs: list[ParsedJob] = field(default_factory=list)
    # Jobs a parser already created by reading a configured source's own page.
    existing: list[Job] = field(default_factory=list)


class AlertParser(Protocol):
    name: str

    def matches(self, message: MailMessage) -> bool: ...

    def parse(self, message: MailMessage, user: User) -> ParseResult: ...


def choose_parser(message: MailMessage) -> AlertParser:
    """The first parser that claims the email; the generic one claims everything."""
    from .generic import GenericAlertParser
    from .linkedin import LinkedInAlertParser

    for parser in (LinkedInAlertParser(), GenericAlertParser()):
        if parser.matches(message):
            return parser
    return GenericAlertParser()
