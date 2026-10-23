"""Rule filter table tests (spec §10.2): each condition on its own, using plain stand-in objects."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from apps.matching.rules import RuleResult, evaluate


@dataclass
class J:
    title: str = "Support Engineer"
    company: str = "Acme Ltd"
    description_text: str = "Help customers."
    work_type: str = "remote"
    locations_raw: list[str] = field(default_factory=list)
    eligible_countries: list[str] = field(default_factory=list)
    eligible_regions: list[str] = field(default_factory=list)
    is_global: bool = True
    eligibility_known: bool = True
    pay_min: Decimal | None = None
    pay_max: Decimal | None = None
    pay_currency: str = ""
    pay_period: str = "unknown"
    extracted: dict[str, Any] | None = None


@dataclass
class T:
    target_titles: list[str] = field(default_factory=lambda: ["support"])
    include_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    work_types: list[str] = field(default_factory=lambda: ["remote"])
    locations: list[str] = field(default_factory=list)
    min_pay_amount: Decimal | None = None
    min_pay_currency: str = "USD"
    min_pay_period: str = "hour"
    excluded_companies: list[str] = field(default_factory=list)


def run(
    job: J | None = None,
    track: T | None = None,
    *,
    country: str = "KE",
    skills: list[str] | None = None,
) -> RuleResult:
    return evaluate(job or J(), track or T(), country=country, skill_names=skills or [])


def has(result: RuleResult, text: str) -> bool:
    return any(text in reason for reason in result.reasons)


def test_a_matching_job_passes_with_readable_reasons() -> None:
    result = run()
    assert result.passed and has(result, "eligible: global") and has(result, "keyword: support")
    assert has(result, "work type: remote") and has(result, "no minimum pay set")


# --- 1. eligibility ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "country", "passes", "reason"),
    [
        (J(is_global=True), "KE", True, "eligible: global"),
        (J(is_global=False, eligible_countries=["KE"]), "KE", True, "eligible: KE"),
        (J(is_global=False, eligible_regions=["AFRICA"]), "KE", True, "eligible: KE"),
        (J(is_global=False, eligible_regions=["EMEA"]), "KE", True, "eligible: KE"),
        (J(is_global=False, eligible_countries=["US"]), "KE", False, "not open to KE"),
        (J(is_global=False, eligible_regions=["NA"]), "KE", False, "not open to KE"),
        (J(is_global=False, eligible_countries=["US", "CA"]), "CA", True, "eligible: CA"),
        (J(is_global=False, eligibility_known=False), "KE", True, "eligibility unclear"),
    ],
)
def test_eligibility(job: J, country: str, passes: bool, reason: str) -> None:
    result = run(job, country=country)
    assert result.passed is passes and has(result, reason)


# --- 2. work type and location --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job_type", "wanted", "passes"),
    [
        ("remote", ["remote"], True),
        ("remote", ["onsite"], False),
        ("remote", ["remote", "hybrid"], True),
        ("hybrid", ["remote"], False),
        ("unknown", ["remote"], True),
        ("", ["remote"], True),
        ("remote", [], True),  # no preference set
    ],
)
def test_work_type(job_type: str, wanted: list[str], passes: bool) -> None:
    result = run(J(work_type=job_type), T(work_types=wanted))
    assert result.passed is passes
    if job_type in ("", "unknown"):
        assert has(result, "work type unknown")


@pytest.mark.parametrize(
    ("locations", "wanted", "passes", "reason"),
    [
        (["Nairobi, Kenya"], ["nairobi"], True, "location matches"),
        (["Nairobi"], ["Nairobi, Kenya"], True, "location matches"),  # either contains the other
        (["Mombasa"], ["Nairobi"], False, "does not match"),
        ([], ["Nairobi"], True, "location unknown"),
        (["Anywhere"], [], True, "no location preference"),
    ],
)
def test_onsite_and_hybrid_need_a_matching_location(
    locations: list[str], wanted: list[str], passes: bool, reason: str
) -> None:
    for work_type in ("onsite", "hybrid"):
        result = run(
            J(work_type=work_type, locations_raw=locations),
            T(work_types=["onsite", "hybrid"], locations=wanted),
        )
        assert result.passed is passes and has(result, reason)


def test_remote_jobs_ignore_the_location_list() -> None:
    assert run(J(work_type="remote", locations_raw=["Mars"]), T(locations=["Nairobi"])).passed


# --- 3. pay -------------------------------------------------------------------------------------------------------------


D = Decimal


@pytest.mark.parametrize(
    ("job", "track", "passes", "reason"),
    [
        (
            J(pay_max=D(20), pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=D(15)),
            True,
            "meets",
        ),
        (
            J(pay_max=D(10), pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=D(15)),
            False,
            "below",
        ),
        (
            J(pay_max=D(15), pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=D(15)),
            True,
            "meets",
        ),
        (
            J(pay_min=D(12), pay_max=None, pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=D(15)),
            False,
            "below",
        ),
        (
            J(pay_min=D(8), pay_max=D(30), pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=D(15)),
            True,
            "meets",
        ),  # the top counts
        (J(), T(min_pay_amount=D(15)), True, "pay unknown"),
        (
            J(pay_max=D(5), pay_currency="EUR", pay_period="hour"),
            T(min_pay_amount=D(15)),
            True,
            "not compared",
        ),
        (
            J(pay_max=D(5), pay_currency="", pay_period="hour"),
            T(min_pay_amount=D(15)),
            True,
            "not compared",
        ),
        (
            J(pay_max=D(60), pay_currency="USD", pay_period="task"),
            T(min_pay_amount=D(15)),
            True,
            "not comparable",
        ),
        (
            J(pay_max=D(5000), pay_currency="USD", pay_period="month"),
            T(min_pay_amount=D(15)),
            True,
            "not comparable",
        ),  # hour vs month
        (
            J(pay_max=D(5000), pay_currency="USD", pay_period="month"),
            T(min_pay_amount=D(72000), min_pay_period="year"),
            False,
            "below",
        ),
        (
            J(pay_max=D(5000), pay_currency="USD", pay_period="month"),
            T(min_pay_amount=D(40000), min_pay_period="year"),
            True,
            "meets",
        ),
        (
            J(pay_max=D(120000), pay_currency="USD", pay_period="year"),
            T(min_pay_amount=D(8000), min_pay_period="month"),
            True,
            "meets",
        ),
        (
            J(pay_max=D(60000), pay_currency="USD", pay_period="year"),
            T(min_pay_amount=D(8000), min_pay_period="month"),
            False,
            "below",
        ),
        (
            J(pay_max=D(1), pay_currency="usd", pay_period="hour"),
            T(min_pay_amount=D(15), min_pay_currency="USD"),
            False,
            "below",
        ),  # case-insensitive
        (
            J(pay_max=D(1), pay_currency="USD", pay_period="hour"),
            T(min_pay_amount=None),
            True,
            "no minimum",
        ),
    ],
)
def test_pay(job: J, track: T, passes: bool, reason: str) -> None:
    result = run(job, track)
    assert result.passed is passes and has(result, reason)


# --- 4. exclusions ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "excluded", [["Acme"], ["acme ltd"], ["ACME, Inc."], ["Other", "Acme Limited"]]
)
def test_excluded_companies_ignore_case_and_legal_suffixes(excluded: list[str]) -> None:
    result = run(J(company="Acme Ltd"), T(excluded_companies=excluded))
    assert not result.passed and has(result, "excluded company")


def test_other_companies_are_not_excluded() -> None:
    assert run(J(company="Acme Ltd"), T(excluded_companies=["Acmetech", "Beta"])).passed


def test_exclude_keywords_apply_to_the_title_only() -> None:
    assert not run(J(title="Senior Support Engineer"), T(exclude_keywords=["senior"])).passed
    assert has(
        run(J(title="Senior Support Engineer"), T(exclude_keywords=["Senior"])),
        "excluded keyword in title: Senior",
    )
    assert run(
        J(title="Support Engineer", description_text="We are a senior team."),
        T(exclude_keywords=["senior"]),
    ).passed


def test_exclude_keywords_match_whole_words() -> None:
    assert run(J(title="Support Engineer II"), T(exclude_keywords=["intern"])).passed
    assert not run(J(title="Support Intern"), T(exclude_keywords=["intern"])).passed


# --- 5. relevance ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("job", "track", "skills", "passes"),
    [
        (J(title="Support Engineer"), T(target_titles=["support"]), [], True),  # title keyword
        (
            J(title="Engineer", description_text="We need strong customer support skills"),
            T(target_titles=["customer support"]),
            [],
            True,
        ),  # description keyword
        (
            J(title="Engineer", description_text="Fix things"),
            T(target_titles=["support"]),
            [],
            False,
        ),
        (
            J(title="Engineer", description_text="Fix things"),
            T(target_titles=[], include_keywords=["fix"]),
            [],
            True,
        ),
        (
            J(title="Engineer", description_text="Uses Python and SQL daily"),
            T(target_titles=["chef"]),
            ["Python", "SQL"],
            True,
        ),  # two skills
        (
            J(title="Engineer", description_text="Uses Python daily"),
            T(target_titles=["chef"]),
            ["Python", "SQL"],
            False,
        ),  # only one
        (
            J(
                title="Engineer",
                description_text="General duties",
                extracted={"required_skills": ["Python", "SQL"]},
            ),
            T(target_titles=["chef"]),
            ["python", "sql"],
            True,
        ),
        (
            J(title="Engineer", description_text="Going places"),
            T(target_titles=["go"]),
            [],
            False,
        ),  # whole words only
        (J(title="Go Developer"), T(target_titles=["go"]), [], True),
        (J(title="C++ Developer", description_text="x"), T(target_titles=["C++"]), [], True),
        (J(title=".NET Developer", description_text="x"), T(target_titles=[".NET"]), [], True),
        (
            J(title="Engineer", description_text="x"),
            T(target_titles=[], include_keywords=[]),
            [],
            False,
        ),
        (
            J(title="Engineer", description_text="Python SQL"),
            T(target_titles=[], include_keywords=[]),
            ["Python", "SQL"],
            True,
        ),
        (J(title="SUPPORT"), T(target_titles=["support"]), [], True),  # case-insensitive
        (
            J(title="Engineer", description_text="x"),
            T(target_titles=["", "  "]),
            [],
            False,
        ),  # blank terms are ignored
    ],
)
def test_relevance(job: J, track: T, skills: list[str], passes: bool) -> None:
    result = run(job, track, skills=skills)
    assert result.passed is passes
    if not passes:
        assert has(result, "no matching keywords or skills")


def test_relevance_counts_distinct_hits_to_rank_tracks() -> None:
    job = J(title="Customer Support Specialist", description_text="Zendesk and Excel")
    narrow = run(job, T(target_titles=["support"]))
    broad = run(
        job,
        T(target_titles=["support", "customer support", "specialist"]),
        skills=["Zendesk", "Excel"],
    )
    assert broad.relevance > narrow.relevance > 0


def test_every_failure_is_reported_not_just_the_first() -> None:
    job = J(
        title="Senior Chef",
        is_global=False,
        eligible_countries=["US"],
        work_type="onsite",
        company="Acme Ltd",
    )
    result = run(
        job, T(target_titles=["support"], exclude_keywords=["senior"], excluded_companies=["Acme"])
    )
    assert not result.passed
    for text in (
        "not open to KE",
        "work type onsite not wanted",
        "excluded company",
        "excluded keyword",
        "no matching keywords",
    ):
        assert has(result, text)


def test_a_failing_job_still_explains_itself() -> None:
    assert run(J(work_type="onsite"), T(work_types=["remote"])).reasons
