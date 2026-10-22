"""CV file validation and text extraction, plus the import pipeline (parse -> apply)."""

import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.files.base import ContentFile

from apps.llm.models import LLMCall, UsageQuota
from apps.llm.providers.fake import FakeProvider
from apps.llm.schemas import ParsedBullet, ParsedCV, ParsedExperience, ParsedSkill
from apps.profiles import cv_files, services
from apps.profiles.exceptions import ImportStateError, InvalidCVFile, NotOwner, UnreadableCV
from apps.profiles.models import (
    Certification,
    CVImport,
    Education,
    Experience,
    ExperienceBullet,
    Profile,
    Project,
    Skill,
)
from tests import files
from tests.factories import ExperienceFactory, SkillFactory, UserFactory
from tests.logcapture import ListHandler

pytestmark = pytest.mark.django_db


# --- validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "content", "kind"),
    [("cv.pdf", files.pdf_bytes(), "pdf"), ("CV.DOCX", files.docx_bytes(), "docx")],
    ids=["pdf", "docx-uppercase-extension"],
)
def test_valid_files_are_accepted_case_insensitively(name: str, content: bytes, kind: str) -> None:
    assert cv_files.validate_upload(name, content) == kind


@pytest.mark.parametrize(
    ("name", "content", "fragment"),
    [
        ("cv.txt", b"hello", "PDF or DOCX"),
        ("cv", b"hello", "PDF or DOCX"),
        ("cv.exe", files.pdf_bytes(), "PDF or DOCX"),
        ("cv.pdf", b"", "empty"),
        ("cv.pdf", b"<html>not a pdf</html>", "real PDF"),
        ("cv.docx", files.pdf_bytes(), "real DOCX"),  # PDF bytes renamed to .docx
        ("cv.pdf", files.docx_bytes(), "real PDF"),  # DOCX bytes renamed to .pdf
        ("cv.docx", files.zip_without_document_xml(), "real DOCX"),
        ("cv.docx", b"PK\x03\x04garbage", "real DOCX"),
    ],
    ids=[
        "txt",
        "no-extension",
        "exe",
        "empty",
        "html-as-pdf",
        "pdf-as-docx",
        "docx-as-pdf",
        "zip-without-document-xml",
        "garbage-zip",
    ],
)
def test_wrong_type_or_magic_bytes_are_rejected(name: str, content: bytes, fragment: str) -> None:
    with pytest.raises(InvalidCVFile) as info:
        cv_files.validate_upload(name, content)
    assert fragment in info.value.message


def test_oversized_files_are_rejected(settings) -> None:  # type: ignore[no-untyped-def]
    settings.CV_MAX_UPLOAD_BYTES = 1000
    with pytest.raises(InvalidCVFile, match="too large"):
        cv_files.validate_upload("cv.pdf", b"%PDF-" + b"0" * 2000)


def test_a_decompression_bomb_docx_is_rejected() -> None:
    bomb = files.docx_zip_bomb()
    assert len(bomb) < django_settings.CV_MAX_UPLOAD_BYTES  # small on the wire
    with pytest.raises(InvalidCVFile, match="real DOCX"):
        cv_files.validate_upload("cv.docx", bomb)


# --- extraction --------------------------------------------------------------------------------------


def test_pdf_text_is_extracted() -> None:
    text = cv_files.extract_text(files.pdf_bytes(), "pdf")
    assert "IT Support Officer" in text and "Active Directory" in text


def test_docx_text_includes_paragraphs_and_tables_in_order() -> None:
    content = files.docx_bytes(
        ["Heading line that is long enough to count as real text for the reader here."],
        table=[["Skills", "Helpdesk"], ["Languages", "English, Swahili"]],
    )
    text = cv_files.extract_text(content, "docx")
    assert text.index("Heading line") < text.index("Helpdesk")
    assert "Languages | English, Swahili" in text


def test_scanned_pdf_gives_a_friendly_error() -> None:
    with pytest.raises(UnreadableCV) as info:
        cv_files.extract_text(files.blank_pdf_bytes(), "pdf")
    assert "scan" in info.value.message and "DOCX" in info.value.message


def test_password_protected_pdf_is_reported() -> None:
    with pytest.raises(UnreadableCV, match="password"):
        cv_files.extract_text(files.encrypted_pdf_bytes(), "pdf")


def test_corrupt_pdf_is_reported_not_crashed() -> None:
    with pytest.raises(UnreadableCV):
        cv_files.extract_text(b"%PDF-1.4\nthis is not really a pdf", "pdf")


def test_nearly_empty_docx_is_reported() -> None:
    with pytest.raises(UnreadableCV, match="almost no text"):
        cv_files.extract_text(files.docx_bytes(["Hi"]), "docx")


# --- create_cv_import --------------------------------------------------------------------------------


def test_upload_is_stored_privately_under_a_random_name() -> None:
    user = UserFactory()
    cv = services.create_cv_import(user, "../../My CV (final).pdf", files.pdf_bytes())

    stored = Path(cv.file.path)
    assert stored.parent == Path(django_settings.PRIVATE_MEDIA_ROOT) / "cv"
    assert stored.suffix == ".pdf" and "final" not in stored.name and ".." not in cv.file.name
    assert "/" not in cv.original_name and "\\" not in cv.original_name
    assert cv.status == CVImport.Status.UPLOADED


def test_scanned_upload_is_refused_immediately_and_nothing_is_saved() -> None:
    with pytest.raises(UnreadableCV):
        services.create_cv_import(UserFactory(), "scan.pdf", files.blank_pdf_bytes())
    assert CVImport.objects.count() == 0


def test_invalid_upload_creates_nothing() -> None:
    with pytest.raises(InvalidCVFile):
        services.create_cv_import(UserFactory(), "cv.pdf", b"not a pdf")
    assert CVImport.objects.count() == 0


# --- parsing -----------------------------------------------------------------------------------------


def _upload(user, django_capture_on_commit_callbacks, name="cv.pdf", content=None):  # type: ignore[no-untyped-def]
    with django_capture_on_commit_callbacks(execute=True):
        cv = services.create_cv_import(user, name, content or files.pdf_bytes())
    cv.refresh_from_db()
    return cv


def test_parsing_runs_after_commit_and_stores_the_review_data(
    django_capture_on_commit_callbacks, handler: ListHandler
) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    cv = _upload(user, django_capture_on_commit_callbacks)

    assert cv.status == CVImport.Status.PARSED
    assert cv.parsed_data["headline"] == "IT Support Specialist"
    assert [e["company"] for e in cv.parsed_data["experiences"]] == ["Acme Ltd", "Beta Networks"]
    call = LLMCall.objects.get()
    assert call.task == "parse_cv" and call.user == user and call.success
    assert handler.events("cv_import_parsed")
    # No CV content in the logs.
    assert "Jane Doe" not in "".join(handler.lines) and "Acme" not in "".join(handler.lines)


def test_the_prompt_wraps_the_cv_as_untrusted_text(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    _upload(UserFactory(), django_capture_on_commit_callbacks)
    sent = fake_llm.calls[0]
    assert "<cv>" in sent["messages"][0]["content"] and "</cv>" in sent["messages"][0]["content"]
    assert "Active Directory" in sent["messages"][0]["content"]
    assert "Never add metrics" in sent["system"] or "Never infer" in sent["system"]


def test_parse_is_idempotent(fake_llm: FakeProvider, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    cv = _upload(UserFactory(), django_capture_on_commit_callbacks)
    services.run_cv_parse(cv.pk)
    services.run_cv_parse(cv.pk)
    assert len(fake_llm.calls) == 1 and LLMCall.objects.count() == 1


def test_bad_llm_output_fails_the_import_with_a_friendly_message(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["not json", "still not json"])
    cv = _upload(UserFactory(), django_capture_on_commit_callbacks)
    assert cv.status == CVImport.Status.FAILED
    assert "unusable answer" in cv.error and cv.parsed_data is None


def test_quota_exceeded_fails_the_import_without_calling_the_provider(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UsageQuota.objects.create(user=user, monthly_cost_usd_cap=Decimal("0"))
    cv = _upload(user, django_capture_on_commit_callbacks)
    assert cv.status == CVImport.Status.FAILED and "limit" in cv.error
    assert fake_llm.calls == []


def test_a_file_that_disappears_fails_gracefully(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    with django_capture_on_commit_callbacks(execute=False):
        cv = services.create_cv_import(user, "cv.pdf", files.pdf_bytes())
    Path(cv.file.path).unlink()
    services.run_cv_parse(cv.pk)
    cv.refresh_from_db()
    assert cv.status == CVImport.Status.FAILED


# --- the fabrication guard ------------------------------------------------------------------------


def _parsed() -> ParsedCV:
    return ParsedCV(
        experiences=[
            ParsedExperience(
                title="Agent",
                company="Acme",
                bullets=[
                    ParsedBullet(
                        text="Handled calls",
                        metrics="reduced waits by 40%",
                        skills=["Telephony", "Zendesk"],
                    ),
                    ParsedBullet(text="Trained staff", metrics="trained 12 people", skills=[]),
                ],
            )
        ],
        skills=[ParsedSkill(name="Zendesk"), ParsedSkill(name="Kubernetes")],
    )


def test_unsupported_metrics_and_skills_are_dropped() -> None:
    source = "I handled calls in Zendesk and trained 12 people at Acme."
    cleaned, dropped = services.sanitise_parsed(_parsed(), source)
    first, second = cleaned.experiences[0].bullets
    assert first.metrics is None  # "40" is not in the CV
    assert first.skills == ["Zendesk"]  # "Telephony" is not in the CV
    assert second.metrics == "trained 12 people"  # "12" is in the CV
    assert [s.name for s in cleaned.skills] == ["Zendesk"]
    assert dropped == 3


def test_sanitise_does_not_modify_its_input() -> None:
    parsed = _parsed()
    services.sanitise_parsed(parsed, "nothing relevant here")
    assert parsed.experiences[0].bullets[0].metrics == "reduced waits by 40%"


def test_the_fixture_cv_survives_sanitising_intact(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    cv = _upload(UserFactory(), django_capture_on_commit_callbacks)
    skills = {s["name"] for s in cv.parsed_data["skills"]}
    assert skills == {"Helpdesk", "Active Directory", "Networking", "Customer service"}
    metrics = cv.parsed_data["experiences"][0]["bullets"][0]["metrics"]
    assert metrics == "handled about 30 tickets per day"


# --- dates ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2021-03-15", datetime.date(2021, 3, 15)),
        ("2021-03", datetime.date(2021, 3, 1)),
        ("2021", datetime.date(2021, 1, 1)),
        ("Mar 2021", datetime.date(2021, 3, 1)),
        ("March 2021", datetime.date(2021, 3, 1)),
        ("Present", None),
        ("to date", None),
        ("", None),
        (None, None),
        ("sometime", None),
        ("2021-13", None),
    ],
)
def test_parse_partial_date(raw: str | None, expected: datetime.date | None) -> None:
    assert services.parse_partial_date(raw) == expected


# --- applying --------------------------------------------------------------------------------------


@pytest.fixture
def parsed_import(django_capture_on_commit_callbacks):  # type: ignore[no-untyped-def]
    user = UserFactory()
    return user, _upload(user, django_capture_on_commit_callbacks)


def test_replace_populates_the_profile(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    result = services.apply_import(user, cv, mode="replace")

    assert (result.experiences, result.bullets, result.projects) == (2, 3, 1)
    assert (result.education, result.certifications, result.skills) == (1, 1, 4)
    profile = Profile.objects.get(user=user)
    assert profile.headline == "IT Support Specialist"
    assert profile.public_email == "jane.doe@example.com"
    assert profile.years_experience == Decimal("4.0")
    assert profile.links == [{"label": "LinkedIn", "url": "https://linkedin.com/in/janedoe"}]

    acme = Experience.objects.get(user=user, company="Acme Ltd")
    assert acme.start_date == datetime.date(2021, 3, 1) and acme.end_date is None
    assert acme.employment_type == "full_time"
    first = acme.bullets.get(order=0)
    assert first.metrics == "handled about 30 tickets per day"
    assert [s.name for s in first.skills.all()] == ["Helpdesk"]
    beta = Experience.objects.get(user=user, company="Beta Networks")
    assert beta.start_date == datetime.date(2020, 1, 1) and beta.end_date == datetime.date(
        2021, 2, 1
    )
    assert Education.objects.get(user=user).end_date == datetime.date(2020, 1, 1)
    assert Skill.objects.get(user=user, name="Customer service").category == "soft"
    assert Certification.objects.get(user=user).status == "earned"
    assert [e.order for e in Experience.objects.for_user(user)] == [0, 1]

    cv.refresh_from_db()
    assert cv.status == CVImport.Status.APPLIED and cv.applied_at is not None


def test_applying_bumps_the_version_once(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    before = services.get_profile(user).version
    services.apply_import(user, cv, mode="replace")
    assert services.get_profile(user).version == before + 1


def test_replace_clears_existing_items_first(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    old = ExperienceFactory(user=user, title="Old job")
    SkillFactory(user=user, name="Old skill")
    services.apply_import(user, cv, mode="replace")
    assert not Experience.objects.filter(pk=old.pk).exists()
    assert not Skill.objects.filter(user=user, name="Old skill").exists()


def test_merge_adds_only_new_items_and_keeps_existing_ones(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    mine = ExperienceFactory(
        user=user,
        title="IT Support Officer",
        company="acme ltd",
        start_date=datetime.date(2021, 3, 1),
    )
    SkillFactory(user=user, name="helpdesk")
    profile = services.get_profile(user)
    profile.headline = "My own headline"
    profile.save()

    result = services.apply_import(user, cv, mode="merge")

    assert result.experiences == 1  # only Beta Networks; Acme matched ignoring case
    assert (
        Experience.objects.filter(pk=mine.pk).exists()
        and Experience.objects.for_user(user).count() == 2
    )
    assert result.skills == 3  # "helpdesk" already existed
    assert Skill.objects.for_user(user).filter(name__iexact="helpdesk").count() == 1
    profile.refresh_from_db()
    assert profile.headline == "My own headline"  # merge never overwrites filled basics
    assert profile.summary.startswith("IT support professional")  # but fills empty ones


def test_merge_twice_adds_nothing_the_second_time(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    first = _upload(user, django_capture_on_commit_callbacks)
    services.apply_import(user, first, mode="merge")
    second = _upload(user, django_capture_on_commit_callbacks)
    result = services.apply_import(user, second, mode="merge")
    assert (result.experiences, result.skills, result.education, result.projects) == (0, 0, 0, 0)
    assert Experience.objects.for_user(user).count() == 2


def test_selection_limits_what_is_added(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    selection = services.Selection(
        experiences={1}, projects=set(), education=set(), certifications=set(), skills={0}
    )
    result = services.apply_import(user, cv, mode="replace", selection=selection)
    assert [e.company for e in Experience.objects.for_user(user)] == ["Beta Networks"]
    assert (result.projects, result.education, result.certifications, result.skills) == (0, 0, 0, 1)
    assert not Project.objects.for_user(user).exists()


def test_confirmed_basics_override_the_parsed_ones(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    services.apply_import(
        user,
        cv,
        mode="replace",
        basics={"headline": "Edited headline", "years_experience": Decimal("7.5")},
    )
    profile = Profile.objects.get(user=user)
    assert profile.headline == "Edited headline" and profile.years_experience == Decimal("7.5")


def test_unsafe_or_oversized_values_are_cleaned_on_apply() -> None:
    user = UserFactory()
    cv = CVImport(
        user=user,
        status=CVImport.Status.PARSED,
        parsed_data=ParsedCV.model_validate(
            {
                "email": "not-an-email",
                "links": [
                    {"label": "x", "url": "javascript:alert(1)"},
                    {"label": "ok", "url": "https://ok.example"},
                ],
                "experiences": [
                    {
                        "title": "T" * 500,
                        "company": "C",
                        "employment_type": "weird",
                        "bullets": [{"text": "B" * 900}],
                    }
                ],
                "skills": [
                    {"name": "Python", "category": "nonsense", "level": "godlike", "years": 500}
                ],
            }
        ).model_dump(),
    )
    cv.file.save("x.pdf", ContentFile(files.pdf_bytes()))
    cv.save()

    services.apply_import(user, cv, mode="replace")

    profile = Profile.objects.get(user=user)
    assert profile.public_email == ""
    assert profile.links == [{"label": "ok", "url": "https://ok.example"}]
    experience = Experience.objects.get(user=user)
    assert len(experience.title) == 150 and experience.employment_type == ""
    assert len(ExperienceBullet.objects.get(user=user).text) == 500
    skill = Skill.objects.get(user=user)
    assert (skill.category, skill.level, skill.years) == ("technical", "", None)


def test_apply_checks_ownership_state_and_mode(parsed_import) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    with pytest.raises(NotOwner):
        services.apply_import(UserFactory(), cv, mode="replace")
    with pytest.raises(ValueError):
        services.apply_import(user, cv, mode="overwrite-everything")
    services.apply_import(user, cv, mode="replace")
    with pytest.raises(ImportStateError):  # already applied
        services.apply_import(user, cv, mode="replace")


def test_a_failed_import_cannot_be_applied(
    fake_llm: FakeProvider, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["bad", "worse"])
    user = UserFactory()
    cv = _upload(user, django_capture_on_commit_callbacks)
    with pytest.raises(ImportStateError):
        services.apply_import(user, cv, mode="replace")


def test_apply_is_atomic(parsed_import, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    user, cv = parsed_import
    ExperienceFactory(user=user, title="Keep me")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("fail midway")

    monkeypatch.setattr(services, "_apply_projects", boom)
    with pytest.raises(RuntimeError):
        services.apply_import(user, cv, mode="replace")
    assert Experience.objects.for_user(user).filter(title="Keep me").exists()  # replace rolled back
    cv.refresh_from_db()
    assert cv.status == CVImport.Status.PARSED
