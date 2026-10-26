"""Turns a kit into documents (spec §12.6).

Facts (name, contact details, companies, job titles, dates, institutions) are read from the
database by ID at render time. The model's `cv_data` only chooses, orders and rephrases, so
nothing in a document's factual fields can be invented. Rendering never calls an LLM.
"""

import datetime
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.template import Context, Engine
from django.utils import timezone

from apps.accounts.models import UserSettings
from apps.llm.schemas import CVData
from apps.profiles.models import Certification, Education, Experience, Project, Skill
from apps.profiles.serializers import parse_ref

from .exceptions import RenderError
from .models import Kit

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates_docs"
_engine = Engine(dirs=[str(TEMPLATE_DIR)], autoescape=True)

DOC_CV, DOC_COVER = "cv", "cover_letter"
FORMATS = ("pdf", "docx")
# Download key -> (document, format). The keys are what URLs and the UI use.
DOWNLOADS: dict[str, tuple[str, str]] = {
    "cv_pdf": (DOC_CV, "pdf"),
    "cv_docx": (DOC_CV, "docx"),
    "cover_letter_pdf": (DOC_COVER, "pdf"),
    "cover_letter_docx": (DOC_COVER, "docx"),
}
DOCX_TEMPLATES = {DOC_CV: "cv_classic.docx", DOC_COVER: "cover_letter.docx"}
HTML_TEMPLATES = {DOC_CV: "cv_classic.html", DOC_COVER: "cover_letter.html"}


# --- context ---------------------------------------------------------------------------------


def _month(value: datetime.date | None) -> str:
    return value.strftime("%b %Y") if value else ""


def date_range(start: datetime.date | None, end: datetime.date | None, *, current: bool) -> str:
    left, right = _month(start), ("Present" if current else _month(end))
    if left and right:
        return f"{left} - {right}"
    return left or right


def _ids(values: list[str], prefix: str) -> list[int]:
    found: list[int] = []
    for value in values:
        parsed = parse_ref(value)
        if parsed and parsed[0] == prefix:
            found.append(parsed[1])
    return found


@dataclass(frozen=True)
class Sender:
    name: str
    contact: list[str]


def sender_for(kit: Kit) -> Sender:
    """The user's name and contact line, from their account and profile (never generated)."""
    from apps.profiles.services import get_profile

    profile = get_profile(kit.user)
    parts = [profile.phone, profile.public_email, profile.location_text]
    parts += [str(link.get("url", "")) for link in profile.links if isinstance(link, dict)]
    return Sender(name=kit.user.full_name.strip(), contact=[p.strip() for p in parts if p.strip()])


def build_cv_context(kit: Kit) -> dict[str, Any]:
    """Everything the CV templates need. Missing or deleted profile items are skipped."""
    cv = CVData.model_validate(kit.cv_data)
    user = kit.user
    sender = sender_for(kit)

    skills_by_id = {s.pk: s.name for s in Skill.objects.for_user(user)}
    skills = [skills_by_id[i] for i in _ids(cv.skills, "SKL") if i in skills_by_id]

    exp_rows = {e.pk: e for e in Experience.objects.for_user(user)}
    experiences: list[dict[str, Any]] = []
    for index, item in enumerate(cv.experiences):
        parsed = parse_ref(item.experience_id)
        row = exp_rows.get(parsed[1]) if parsed and parsed[0] == "EXP" else None
        if row is None:
            continue
        experiences.append(
            {
                "index": index,  # position in cv_data, which the edit form addresses
                "id": item.experience_id,
                "title": row.title,
                "company": row.company,
                "location": row.location,
                "dates": date_range(row.start_date, row.end_date, current=row.end_date is None),
                "bullets": [b.text for b in item.bullets],
            }
        )

    project_rows = {p.pk: p for p in Project.objects.for_user(user)}
    projects: list[dict[str, Any]] = []
    for index, entry in enumerate(cv.projects):
        parsed = parse_ref(entry.project_id)
        project = project_rows.get(parsed[1]) if parsed and parsed[0] == "PRJ" else None
        if project is None:
            continue
        projects.append(
            {
                "index": index,
                "id": entry.project_id,
                "name": project.name,
                "role": project.role,
                "url": project.url,
                "bullets": [b.text for b in entry.bullets],
            }
        )

    education_rows = {e.pk: e for e in Education.objects.for_user(user)}
    education = []
    for pk in _ids(cv.education_ids, "EDU"):
        edu = education_rows.get(pk)
        if edu is None:
            continue
        title = " in ".join(p for p in (edu.qualification, edu.field) if p)
        education.append(
            {
                "title": title or edu.institution,
                "institution": edu.institution if title else "",
                "dates": date_range(edu.start_date, edu.end_date, current=False),
                "grade": edu.grade,
            }
        )

    cert_rows = {c.pk: c for c in Certification.objects.for_user(user)}
    certifications = []
    for pk in _ids(cv.certification_ids, "CRT"):
        cert = cert_rows.get(pk)
        if cert is None:
            continue
        certifications.append(
            {
                "name": cert.name,
                "issuer": cert.issuer,
                "issued": _month(cert.issued_on),
                "in_progress": cert.status == Certification.Status.IN_PROGRESS,
            }
        )

    return {
        "name": sender.name or "Your name",
        "contact": sender.contact,
        "contact_line": " | ".join(sender.contact),
        "headline": cv.headline,
        "summary": cv.summary,
        "skills": skills,
        "skills_line": ", ".join(skills),
        "experiences": experiences,
        "projects": projects,
        "education": education,
        "certifications": certifications,
    }


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


def build_letter_context(kit: Kit) -> dict[str, Any]:
    sender = sender_for(kit)
    tz_name = (
        UserSettings.objects.for_user(kit.user).values_list("timezone", flat=True).first()
        or "Africa/Nairobi"
    )
    try:
        from zoneinfo import ZoneInfo

        today = timezone.now().astimezone(ZoneInfo(tz_name)).date()
    except Exception:  # an unknown zone name must not stop a document being produced
        today = timezone.now().date()
    return {
        "name": sender.name or "Your name",
        "contact_line": " | ".join(sender.contact),
        "date": f"{today.day} {today.strftime('%B %Y')}",
        "company": kit.job.company,
        "job_title": kit.job.title,
        "salutation": "Dear Hiring Team,",
        "paragraphs": split_paragraphs(kit.cover_letter_text),
        "closing": "Kind regards,",
    }


# --- rendering -------------------------------------------------------------------------------


def render_html(document: str, context: dict[str, Any]) -> str:
    return _engine.get_template(HTML_TEMPLATES[document]).render(Context(context))


def html_to_pdf(html: str) -> bytes:
    """HTML to PDF bytes. WeasyPrint is imported here so the app runs where Pango is missing."""
    try:
        from weasyprint import HTML
    except OSError as exc:  # Pango/GTK libraries not installed
        raise RenderError("PDF rendering is not available on this server.") from exc
    try:
        return bytes(HTML(string=html, base_url=str(TEMPLATE_DIR) + "/").write_pdf())
    except Exception as exc:
        raise RenderError("A PDF could not be produced.") from exc


def render_docx(document: str, context: dict[str, Any]) -> bytes:
    from docxtpl import DocxTemplate

    path = TEMPLATE_DIR / DOCX_TEMPLATES[document]
    if not path.is_file():
        raise RenderError("The document template is missing.")
    try:
        template = DocxTemplate(str(path))
        template.render(context, autoescape=True)
        buffer = io.BytesIO()
        template.save(buffer)
    except Exception as exc:
        raise RenderError("A Word document could not be produced.") from exc
    return buffer.getvalue()


def render_all(kit: Kit) -> tuple[dict[str, bytes], RenderError | None]:
    """The kit's files keyed by download key, plus the first rendering error if any. Each file is
    rendered on its own, so a missing PDF engine still leaves the Word documents."""
    cv_context = build_cv_context(kit)
    letter_context = build_letter_context(kit)
    jobs = {
        "cv_pdf": lambda: html_to_pdf(render_html(DOC_CV, cv_context)),
        "cv_docx": lambda: render_docx(DOC_CV, cv_context),
        "cover_letter_pdf": lambda: html_to_pdf(render_html(DOC_COVER, letter_context)),
        "cover_letter_docx": lambda: render_docx(DOC_COVER, letter_context),
    }
    files: dict[str, bytes] = {}
    first_error: RenderError | None = None
    for key, render in jobs.items():
        try:
            files[key] = render()
        except RenderError as exc:
            first_error = first_error or exc
    return files, first_error


# --- file names ------------------------------------------------------------------------------


def _ascii_word(value: str, limit: int = 30) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Za-z0-9]+", "-", folded).strip("-")[:limit]


def download_filename(kit: Kit, key: str) -> str:
    """`<First>_<Last>_CV_<Company>.pdf`, ASCII only (spec §12.6)."""
    document, fmt = DOWNLOADS[key]
    names = kit.user.full_name.split()
    person = [_ascii_word(n) for n in ([names[0], names[-1]] if len(names) > 1 else names)]
    label = "CV" if document == DOC_CV else "CoverLetter"
    parts = [p for p in (*person, label, _ascii_word(kit.job.company)) if p]
    if not person:
        parts.insert(0, "Candidate")
    return "_".join(parts) + f".{fmt}"
