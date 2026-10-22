"""Pydantic output schemas for LLM tasks. Each later phase adds its own (JobExtraction in
Phase 4, CV data in Phase 7, and so on); Phase 2 only needs the smoke-test schema."""

from typing import Literal

from pydantic import BaseModel


class SmokeResult(BaseModel):
    ok: bool
    message: str


# --- CV parsing (task `parse_cv`, spec §15.4) --------------------------------------------------
# Deliberately lenient (plain strings for dates and enums): the profile service normalises them,
# so a slightly off value does not cost a repair retry.


class ParsedLink(BaseModel):
    label: str = ""
    url: str


class ParsedBullet(BaseModel):
    text: str
    metrics: str | None = None
    skills: list[str] = []


class ParsedExperience(BaseModel):
    title: str
    company: str
    location: str | None = None
    employment_type: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_current: bool = False
    description: str | None = None
    bullets: list[ParsedBullet] = []


class ParsedProject(BaseModel):
    name: str
    role: str | None = None
    url: str | None = None
    description: str | None = None
    technologies: list[str] = []
    start_date: str | None = None
    end_date: str | None = None
    highlights: list[str] = []


class ParsedEducation(BaseModel):
    institution: str
    qualification: str | None = None
    field: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    grade: str | None = None
    notes: str | None = None


class ParsedCertification(BaseModel):
    name: str
    issuer: str | None = None
    issued_on: str | None = None
    expires_on: str | None = None
    credential_url: str | None = None
    status: str = "earned"


class ParsedSkill(BaseModel):
    name: str
    category: str = "technical"
    level: str | None = None
    years: float | None = None


class ParsedCV(BaseModel):
    headline: str | None = None
    summary: str | None = None
    phone: str | None = None
    email: str | None = None
    location: str | None = None
    links: list[ParsedLink] = []
    years_experience: float | None = None
    experiences: list[ParsedExperience] = []
    projects: list[ParsedProject] = []
    education: list[ParsedEducation] = []
    certifications: list[ParsedCertification] = []
    skills: list[ParsedSkill] = []


# --- job extraction (tasks `extract_job` and `extract_page`, spec §11.6) --------------------------


class JobExtraction(BaseModel):
    summary: str  # <= 40 words
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unknown"]
    required_skills: list[str]  # max 15, normalised names
    nice_to_have_skills: list[str]  # max 10
    responsibilities: list[str]  # max 8, short
    requirements: list[str]  # max 8, short (degrees, years, languages, hours/week...)
    hours_commitment: str | None = None
    pay_text: str | None = None  # only if found in the text
    locations: list[str] = []  # only if found in the text
    work_type: Literal["remote", "onsite", "hybrid", "unknown"] = "unknown"
    application_steps: list[str] = []  # e.g. "AI interview", "coding test", "ID verification"
    red_flags: list[str] = []  # scam-like signals, empty if none


class ExtractedPage(BaseModel):
    """What the LLM reads off a job page for `llm_extract` sources (task `extract_page`)."""

    is_job_posting: bool
    title: str = ""
    company: str = ""
    description: str = ""
    pay_text: str | None = None
    locations: list[str] = []
    work_type: Literal["remote", "onsite", "hybrid", "unknown"] = "unknown"
    employment_type: str | None = None
    apply_url: str | None = None
