"""Pydantic output schemas for LLM tasks. Each later phase adds its own (JobExtraction in
Phase 4, CV data in Phase 7, and so on); Phase 2 only needs the smoke-test schema."""

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
