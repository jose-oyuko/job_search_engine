"""Shared setup for kit tests: a user with a real profile, a scripted fake LLM, PDF fallback."""

import json
from typing import Any

import pytest

from apps.accounts.models import UserSettings
from apps.jobs.models import Job
from apps.kits import documents
from apps.llm.providers.fake import FakeProvider
from apps.profiles import services as profile_services
from apps.profiles.serializers import ref
from tests.factories import (
    BulletFactory,
    CertificationFactory,
    EducationFactory,
    ExperienceFactory,
    JobFactory,
    ProjectFactory,
    SkillFactory,
    TrackFactory,
    UserFactory,
)


def _pdf_works() -> bool:
    try:
        from weasyprint import HTML

        return bytes(HTML(string="<p>x</p>").write_pdf())[:4] == b"%PDF"
    except Exception:  # missing Pango/GTK libraries raise OSError at import
        return False


PDF_AVAILABLE = _pdf_works()
requires_pdf = pytest.mark.skipif(not PDF_AVAILABLE, reason="WeasyPrint needs the Pango libraries")


@pytest.fixture
def pdf_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Where Pango is not installed, stand in for the PDF step so the rest still runs."""
    if not PDF_AVAILABLE:
        monkeypatch.setattr(documents, "html_to_pdf", lambda html: b"%PDF-1.7 stand-in")


class Ids:
    """Profile item IDs of a test user, in the form the kit generator cites them."""

    exp: str
    bul1: str
    bul2: str
    exp2: str
    bul3: str
    prj: str
    skl_helpdesk: str
    skl_excel: str
    skl_python: str
    edu: str
    crt: str

    def __init__(self, **values: str) -> None:
        self.__dict__.update(values)


def make_kit_user(*, full_name: str = "Jane Wanjiru Doe") -> tuple[Any, Ids]:
    user = UserFactory(full_name=full_name)
    UserSettings.objects.create(user=user, country="KE", onboarding_completed=True)
    profile = profile_services.get_profile(user)
    profile.headline = "IT support specialist"
    profile.summary = "Support specialist with experience in helpdesk work and small networks."
    profile.phone = "+254700000000"
    profile.public_email = "jane@example.com"
    profile.location_text = "Nairobi, Kenya"
    profile.save()

    helpdesk = SkillFactory(user=user, name="Helpdesk", level="advanced", years=3)
    excel = SkillFactory(user=user, name="Excel", level="intermediate")
    python = SkillFactory(user=user, name="Python", level="basic")

    exp = ExperienceFactory(user=user, title="IT Support Technician", company="Acme Ltd", order=1)
    bul1 = BulletFactory(
        experience=exp,
        text="Resolved support tickets for 120 staff",
        metrics="reduced ticket backlog 40%",
    )
    bul1.skills.add(helpdesk)
    bul2 = BulletFactory(experience=exp, text="Set up Windows laptops for new staff")
    exp2 = ExperienceFactory(user=user, title="Intern", company="Beta Ltd", order=2)
    bul3 = BulletFactory(experience=exp2, text="Wrote guides for the IT team")

    project = ProjectFactory(user=user, name="Inventory tracker", description="A small Excel tool")
    edu = EducationFactory(
        user=user, institution="Nairobi Polytechnic", qualification="Diploma", field="IT"
    )
    cert = CertificationFactory(user=user, name="CompTIA A+", issuer="CompTIA")
    TrackFactory(
        user=user,
        name="IT support",
        is_default=True,
        target_titles=["support"],
        work_types=["remote"],
        cv_emphasis="lead with support experience",
    )
    ids = Ids(
        exp=ref("EXP", exp.pk), bul1=ref("BUL", bul1.pk), bul2=ref("BUL", bul2.pk),
        exp2=ref("EXP", exp2.pk), bul3=ref("BUL", bul3.pk), prj=ref("PRJ", project.pk),
        skl_helpdesk=ref("SKL", helpdesk.pk), skl_excel=ref("SKL", excel.pk),
        skl_python=ref("SKL", python.pk), edu=ref("EDU", edu.pk), crt=ref("CRT", cert.pk),
    )  # fmt: skip
    return user, ids


def make_kit_job(**fields: Any) -> Job:
    defaults: dict[str, Any] = {
        "title": "Remote IT Support Engineer",
        "company": "Globex Corp",
        "description_text": "Support our staff with laptops and tickets. 40 hours a week.",
        "work_type": "remote",
        "is_global": True,
        "eligibility_known": True,
        "extracted": {
            "summary": "Support staff remotely.",
            "required_skills": ["Helpdesk", "Active Directory"],
            "nice_to_have_skills": ["Python"],
        },
    }
    return JobFactory(**{**defaults, **fields})


def good_cv(ids: Ids, **overrides: Any) -> dict[str, Any]:
    """A CV that cites real IDs, uses only numbers from the profile and passes validation."""
    cv: dict[str, Any] = {
        "headline": "IT Support Specialist | Helpdesk, Excel",
        "summary": "Support specialist with helpdesk experience and a focus on clear communication.",
        "summary_source_ids": [ids.exp, ids.skl_helpdesk],
        "skills": [ids.skl_helpdesk, ids.skl_excel],
        "experiences": [
            {
                "experience_id": ids.exp,
                "bullets": [
                    {
                        "text": "Resolved support tickets for 120 staff and cut the backlog by 40%.",
                        "source_ids": [ids.bul1],
                    },
                    {"text": "Set up Windows laptops for new staff.", "source_ids": [ids.bul2]},
                ],
            },
            {
                "experience_id": ids.exp2,
                "bullets": [{"text": "Wrote guides for the IT team.", "source_ids": [ids.bul3]}],
            },
        ],
        "projects": [
            {
                "project_id": ids.prj,
                "bullets": [
                    {"text": "Built a small Excel tool to track stock.", "source_ids": [ids.prj]}
                ],
            }
        ],
        "education_ids": [ids.edu],
        "certification_ids": [ids.crt],
    }
    cv.update(overrides)
    return cv


SENTENCE = "I keep users working and I explain each fix in plain words so it stays fixed. "


def letter_text(words: int = 220) -> str:
    """A clean cover letter body of about `words` words, in three paragraphs."""
    sentence_words = SENTENCE.split()
    pool: list[str] = []
    while len(pool) < words:
        pool += sentence_words
    pool = pool[:words]
    third = words // 3
    parts = [pool[:third], pool[third : 2 * third], pool[2 * third :]]
    return "\n\n".join(" ".join(p) for p in parts)


def interview_json(count: int = 10, *, source_id: str | None = "EXP-99999") -> str:
    return json.dumps(
        {
            "items": [
                {
                    "category": "technical",
                    "question": f"Question {i}?",
                    "why_they_ask": "To see how you work.",
                    "answer_outline": ["Point one", "Point two"],
                    "source_ids": [source_id] if (i == 0 and source_id) else [],
                }
                for i in range(count)
            ]
        }
    )


def screening_json() -> str:
    return json.dumps(
        {
            "answers": [
                {"question": "Why this role?", "answer": "It fits my experience."},
                {"question": "Expected rate", "answer": "Open to discussion."},
            ]
        }
    )


def script_kit(
    fake_llm: FakeProvider,
    cv: dict[str, Any],
    *,
    letter: str | None = None,
    interview: bool = True,
    screening: bool = True,
) -> None:
    """Queue the model replies a clean run consumes, in pipeline order."""
    fake_llm.script.append(json.dumps(cv))
    fake_llm.script.append(letter if letter is not None else letter_text())
    if interview:
        fake_llm.script.append(interview_json())
    if screening:
        fake_llm.script.append(screening_json())
