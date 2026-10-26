"""What notifications say (spec §13.4). Text is built from live data at send time, so nothing
personal is stored in `Notification.payload`."""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.timesince import timesince

from apps.jobs.eligibility import is_eligible
from apps.matching.models import Match

from . import signing
from .gateway import Button, Buttons

MAX_DIGEST_ITEMS = 15


def site_url(path: str = "") -> str:
    return settings.SITE_URL.rstrip("/") + path


def job_url(match: Match) -> str:
    return site_url(f"/app/jobs/{match.job_id}/")


def _link_buttons_allowed() -> bool:
    """Telegram refuses URL buttons that point at localhost or plain http."""
    url = settings.SITE_URL
    return url.startswith("https://") and "localhost" not in url and "127.0.0.1" not in url


def score_dot(score: int | None) -> str:
    if score is None:
        return "⚪"
    return "🟢" if score >= 80 else "🟡" if score >= 60 else "🟠"


def _eligibility(match: Match, country: str) -> str:
    eligible = is_eligible(match.job, country)
    if eligible is None:
        return "Eligibility unclear"
    if eligible:
        return (
            f"Eligible: {country}" if not match.job.is_global else f"Global (eligible: {country})"
        )
    return f"Not open to {country}"


def _age(match: Match) -> str:
    when = match.job.posted_at or match.job.first_seen_at
    return f"{timesince(when, timezone.now()).split(',')[0]} ago"


def _source(match: Match) -> str:
    job = match.job
    return f"via {job.source.display_name}" if job.source else "added by you"


def instant_text(match: Match, country: str, *, kit_ready: bool = False) -> str:
    job = match.job
    score = "?" if match.score is None else str(match.score)
    lines = [
        f"{score_dot(match.score)} {score} · {job.title} — {job.company}",
        " · ".join(
            [
                job.pay_text or "Pay not stated",
                job.get_work_type_display(),
                _eligibility(match, country),
            ]
        ),
    ]
    detail = []
    if match.strengths:
        detail.append(f"Strong: {match.strengths[0]}")
    if match.gaps:
        detail.append(f"Gap: {match.gaps[0]}")
    if detail:
        lines.append(" · ".join(detail))
    lines.append(f"Posted {_age(match)} {_source(match)}")
    if kit_ready:
        lines.append("Your CV and cover letter are ready.")
    lines.append(f"Open: {job_url(match)}")
    return "\n".join(lines)


def instant_buttons(match: Match, *, kit_ready: bool) -> Buttons:
    user_id = match.user_id
    row = []
    if _link_buttons_allowed():
        row.append(Button("Open", url=job_url(match)))
    row += [
        Button("Kit ✓" if kit_ready else "Kit", data=signing.sign_callback("k", match.pk, user_id)),
        Button("Applied ✓", data=signing.sign_callback("a", match.pk, user_id)),
        Button("Not interested", data=signing.sign_callback("n", match.pk, user_id)),
    ]
    return [row]


@dataclass
class DigestContent:
    matches: list[Match]
    more: int
    dashboard_url: str


def digest_line(match: Match) -> str:
    job = match.job
    score = "?" if match.score is None else str(match.score)
    pay = f" · {job.pay_text}" if job.pay_text else ""
    return f"{score_dot(match.score)} {score} · {job.title} — {job.company}{pay}"


def digest_text(content: DigestContent) -> str:
    count = len(content.matches) + content.more
    lines = [f"📬 {count} new match{'es' if count != 1 else ''}", ""]
    for match in content.matches:
        lines += [digest_line(match), job_url(match), ""]
    if content.more:
        lines.append(f"…and {content.more} more on your dashboard: {content.dashboard_url}")
    else:
        lines.append(f"Your dashboard: {content.dashboard_url}")
    return "\n".join(lines)


def email_context(content: DigestContent) -> dict[str, Any]:
    items = [
        {
            "line": digest_line(m),
            "title": m.job.title,
            "company": m.job.company,
            "score": m.score,
            "dot": score_dot(m.score),
            "pay": m.job.pay_text,
            "url": job_url(m),
        }
        for m in content.matches
    ]
    return {
        "items": items,
        "count": len(items) + content.more,
        "more": content.more,
        "dashboard_url": content.dashboard_url,
        "settings_url": site_url("/app/settings/notifications/"),
        "site_name": settings.SITE_NAME,
    }


def render_email(template: str, context: dict[str, Any]) -> tuple[str, str]:
    """`(text, html)` for `notifications/email/<template>.txt` and `.html`."""
    return (
        render_to_string(f"notifications/email/{template}.txt", context),
        render_to_string(f"notifications/email/{template}.html", context),
    )


def kit_ready_text(job_title: str, company: str, url: str) -> str:
    return f"✅ Your application kit for {job_title} — {company} is ready.\n{url}"
