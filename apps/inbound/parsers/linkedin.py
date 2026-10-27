"""LinkedIn job-alert emails (spec §9.2). Everything comes from the email body: title, company,
location and the `/jobs/view/<id>` link. No LinkedIn page is ever fetched."""

import re
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from apps.accounts.models import User
from apps.inbound.mailbox import MailMessage

from . import MAX_JOBS_PER_EMAIL, ParsedJob, ParseResult

_JOB_URL = re.compile(
    r"https?://(?:[\w-]+\.)?linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\s\"'<>]*?-)?(\d{6,})", re.I
)
_SENDER = re.compile(r"@(?:[\w-]+\.)*linkedin\.com$", re.I)
_SEPARATORS = re.compile(r"\s+[·•|]\s+")
# Lines in an alert card that are not the title, company or location.
_NOISE = re.compile(
    r"^(view job|apply( now)?|easy apply|see all jobs?|view all|actively (recruiting|hiring)|"
    r"be an early applicant|promoted|new|\d+\s+(applicants?|connections?|alumni|school alumni).*|"
    r"(\d+\s+)?(minutes?|hours?|days?|weeks?)\s+ago|reposted.*|save|unsubscribe.*|"
    r"you are receiving.*|this email was sent.*|manage .*|help|learn why.*)$",
    re.I,
)
_REMOTE = re.compile(r"\bremote\b", re.I)


def canonical_job_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}"


class LinkedInAlertParser:
    name = "linkedin"

    def matches(self, message: MailMessage) -> bool:
        return bool(_SENDER.search(message.from_address))

    def parse(self, message: MailMessage, user: User) -> ParseResult:
        found = self._from_html(message.html) if message.html else []
        if not found:
            found = self._from_text(message.text)
        jobs = [self._job(job_id, title, lines) for job_id, title, lines in found]
        return ParseResult(jobs=[j for j in jobs if j is not None][:MAX_JOBS_PER_EMAIL])

    # --- building a job from the lines around its link ---------------------------------------

    @staticmethod
    def _job(job_id: str, title: str, lines: list[str]) -> ParsedJob | None:
        details: list[str] = []
        for line in lines:
            for part in _SEPARATORS.split(line):
                part = part.strip()
                if part and not _NOISE.match(part) and part.lower() != title.lower():
                    details.append(part)
        company = details[0] if details else ""
        location = details[1] if len(details) > 1 else ""
        if not title or not company:
            return None
        return ParsedJob(
            title=title[:250],
            company=company[:200],
            url=canonical_job_url(job_id),
            external_id=f"linkedin:{job_id}",
            locations=[location[:150]] if location else [],
            work_type="remote" if _REMOTE.search(location) else "",
        )

    # --- HTML emails ----------------------------------------------------------------------------

    def _from_html(self, html: str) -> list[tuple[str, str, list[str]]]:
        tree = LexborHTMLParser(html)
        by_id: dict[str, list[LexborNode]] = {}
        for anchor in tree.css("a[href]"):
            match = _JOB_URL.search(anchor.attributes.get("href") or "")
            if match:
                by_id.setdefault(match.group(1), []).append(anchor)
        results = []
        for job_id, anchors in by_id.items():
            title = next(
                (t for a in anchors if (t := _clean(a.text())) and not _NOISE.match(t)), ""
            )
            if not title:
                continue
            results.append((job_id, title, self._card_lines(anchors[0], job_id, by_id)))
        return results

    @staticmethod
    def _card_lines(
        anchor: LexborNode, job_id: str, by_id: dict[str, list[LexborNode]]
    ) -> list[str]:
        """The text lines of the smallest block around the link that holds more than the title and
        belongs to this job alone (no other job's link inside it)."""
        node: LexborNode | None = anchor.parent
        best: list[str] = []
        for _ in range(6):
            if node is None:
                break
            other_links = [
                a
                for other_id, anchors in by_id.items()
                if other_id != job_id
                for a in anchors
                if _inside(a, node)
            ]
            if other_links:
                break
            lines = [_clean(x) for x in node.text(separator="\n", strip=True).split("\n")]
            best = [x for x in lines if x]
            if len(best) >= 3:
                break
            node = node.parent
        return best

    # --- plain-text emails ----------------------------------------------------------------------

    @staticmethod
    def _from_text(text: str) -> list[tuple[str, str, list[str]]]:
        lines = [_clean(line) for line in text.splitlines()]
        results = []
        seen: set[str] = set()
        for index, line in enumerate(lines):
            match = _JOB_URL.search(line)
            if not match or match.group(1) in seen:
                continue
            seen.add(match.group(1))
            before = [x for x in lines[:index] if x and not _JOB_URL.search(x)]
            before = [x for x in before if not _NOISE.match(x)]
            if len(before) >= 3:
                title, *details = before[-3:]
                results.append((match.group(1), title, details))
        return results


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _inside(node: LexborNode, ancestor: LexborNode) -> bool:
    current: LexborNode | None = node
    while current is not None:
        if current.mem_id == ancestor.mem_id:
            return True
        current = current.parent
    return False


def is_linkedin_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower().endswith("linkedin.com")
