"""Eligibility table tests (spec §7.3) and scam heuristics (spec §7.7)."""

from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from apps.jobs import eligibility as el
from apps.jobs import scam


@dataclass
class FakeJob:
    eligible_countries: list[str] = field(default_factory=list)
    eligible_regions: list[str] = field(default_factory=list)
    is_global: bool = False
    eligibility_known: bool = False


def job_for(*locations: str) -> FakeJob:
    parsed = el.parse_locations(locations)
    return FakeJob(parsed.countries, parsed.regions, parsed.is_global, parsed.known)


# --- parsing ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "countries", "regions", "is_global"),
    [
        (["Global"], [], [], True),
        (["Worldwide"], [], [], True),
        (["Anywhere in the world"], [], [], True),
        (["GLOBAL"], [], [], True),
        (["Remote (Global)"], [], [], True),
        (["Kenya"], ["KE"], [], False),
        (["KE"], ["KE"], [], False),
        (["kenya"], ["KE"], [], False),
        (["Nairobi, KE"], ["KE"], [], False),
        (["United States"], ["US"], [], False),
        (["US only"], ["US"], [], False),
        (["USA"], ["US"], [], False),
        (["U.S."], ["US"], [], False),
        (["United Kingdom"], ["GB"], [], False),
        (["UK"], ["GB"], [], False),
        (["United Arab Emirates"], ["AE"], [], False),
        (["EMEA"], [], ["EMEA"], False),
        (["Africa"], [], ["AFRICA"], False),
        (["Sub-Saharan Africa"], [], ["AFRICA"], False),
        (["Europe"], [], ["EUROPE"], False),
        (["European Union"], [], ["EU"], False),
        (["North America"], [], ["NA"], False),
        (["NORTH_AMERICA"], [], ["NA"], False),
        (["Latin America"], [], ["LATAM"], False),
        (["LATAM"], [], ["LATAM"], False),
        (["APAC"], [], ["APAC"], False),
        (["Asia-Pacific"], [], ["APAC"], False),
        (["Middle East"], [], ["MENA"], False),
        (["Kenya, Uganda, Tanzania"], ["KE", "UG", "TZ"], [], False),
        (["Kenya", "Uganda"], ["KE", "UG"], [], False),
        (["Kenya; Nigeria / Ghana"], ["KE", "NG", "GH"], [], False),
        (["Trinidad and Tobago"], ["TT"], [], False),
        (["Bosnia and Herzegovina"], ["BA"], [], False),
        (["South Africa"], ["ZA"], [], False),  # not Africa + "South"
        (["Ivory Coast"], ["CI"], [], False),
        (["South Korea"], ["KR"], [], False),
        (["Global", "Kenya"], ["KE"], [], True),
        (["Remote - US or Canada"], ["US", "CA"], [], False),
        (
            ["Bangladesh, Brazil, Colombia, Egypt, Ghana, India, Indonesia, Nigeria, Turkey"],
            ["BD", "BR", "CO", "EG", "GH", "IN", "ID", "NG", "TR"],
            [],
            False,
        ),
    ],
)
def test_parse_locations(
    raw: list[str], countries: list[str], regions: list[str], is_global: bool
) -> None:
    parsed = el.parse_locations(raw)
    assert sorted(parsed.countries) == sorted(countries)
    assert parsed.regions == regions
    assert parsed.is_global is is_global
    assert parsed.known is True


@pytest.mark.parametrize(
    "raw",
    [
        [],
        [""],
        ["   "],
        ["Remote"],
        ["Flexible hours"],
        ["TBD"],
        ["Work from home in your pajamas"],
    ],
)
def test_unknown_locations_are_not_known(raw: list[str]) -> None:
    parsed = el.parse_locations(raw)
    assert parsed.known is False
    assert (parsed.countries, parsed.regions, parsed.is_global) == ([], [], False)


@pytest.mark.parametrize(
    "raw",
    ["Remote - IT support", "Work in IT", "Be in the loop", "Do it", "Open to all", "Join us"],
)
def test_english_words_are_not_mistaken_for_country_codes(raw: str) -> None:
    assert el.parse_locations([raw]).countries == []


def test_a_whole_token_code_is_read_as_a_code() -> None:
    assert el.parse_locations(["IN"]).countries == ["IN"]  # India, as the only token
    assert el.parse_locations(["IT, FR"]).countries == ["IT", "FR"]


def test_results_do_not_repeat_entries() -> None:
    parsed = el.parse_locations(["Kenya", "KE", "Nairobi, Kenya"])
    assert parsed.countries == ["KE"]


# --- is_eligible: the acceptance cases ---------------------------------------------------------------


def test_global_job_is_open_to_everyone() -> None:
    job = job_for("Global")
    assert el.is_eligible(job, "KE") is True
    assert el.is_eligible(job, "US") is True


def test_kenya_job() -> None:
    job = job_for("Kenya")
    assert el.is_eligible(job, "KE") is True
    assert el.is_eligible(job, "ke") is True  # case-insensitive
    assert el.is_eligible(job, "UG") is False


def test_us_only_job() -> None:
    job = job_for("US only")
    assert el.is_eligible(job, "US") is True
    assert el.is_eligible(job, "KE") is False


def test_emea_job_includes_kenya_but_not_the_us() -> None:
    job = job_for("EMEA")
    assert el.is_eligible(job, "KE") is True  # Africa is part of EMEA
    assert el.is_eligible(job, "DE") is True
    assert el.is_eligible(job, "AE") is True
    assert el.is_eligible(job, "US") is False
    assert el.is_eligible(job, "IN") is False


def test_unknown_eligibility_is_none_not_false() -> None:
    job = job_for("Remote")
    assert el.is_eligible(job, "KE") is None
    assert el.is_eligible(FakeJob(), "KE") is None


def test_country_and_region_can_be_combined() -> None:
    job = job_for("Brazil", "Africa")
    assert el.is_eligible(job, "BR") is True
    assert el.is_eligible(job, "KE") is True
    assert el.is_eligible(job, "US") is False


@pytest.mark.parametrize(
    ("region", "inside", "outside"),
    [
        ("AFRICA", "KE", "FR"),
        ("EU", "DE", "GB"),
        ("EUROPE", "GB", "US"),
        ("NA", "MX", "BR"),
        ("LATAM", "BR", "US"),
        ("APAC", "IN", "KE"),
        ("MENA", "EG", "KE"),
        ("EMEA", "KE", "US"),
    ],
)
def test_region_membership(region: str, inside: str, outside: str) -> None:
    job = FakeJob(eligible_regions=[region], eligibility_known=True)
    assert el.is_eligible(job, inside) is True
    assert el.is_eligible(job, outside) is False


def test_the_region_table_covers_the_spec() -> None:
    assert {"AFRICA", "EMEA", "EU", "EUROPE", "NA", "LATAM", "APAC", "MENA"} <= el.REGION_CODES
    assert len(el.REGION_COUNTRIES["AFRICA"]) >= 54
    assert el.REGION_COUNTRIES["NA"] == {"US", "CA", "MX"}
    assert el.REGION_COUNTRIES["EU"] < el.REGION_COUNTRIES["EUROPE"] < el.REGION_COUNTRIES["EMEA"]


def test_every_region_member_is_a_real_iso_code() -> None:
    from apps.core.countries import country_choices

    real = {code for code, _ in country_choices()}
    for region, members in el.REGION_COUNTRIES.items():
        assert members <= real, (region, members - real)


# --- scam heuristics ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "A small registration fee of $50 is required before you start.",
        "You must pay a training fee to access the platform.",
        "Send a refundable deposit to secure your slot.",
        "Upfront payment is needed for onboarding.",
        "There is an application fee.",
        "Purchase your own equipment from us to begin.",
    ],
)
def test_payment_requests_are_flagged(text: str) -> None:
    assert scam.FLAG_ASKS_PAYMENT in scam.detect_scam_flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "Please send a copy of your passport before the interview.",
        "Provide your bank details to apply.",
        "Submit your national ID prior to the first call.",
    ],
)
def test_early_id_requests_are_flagged(text: str) -> None:
    assert scam.FLAG_ASKS_ID_EARLY in scam.detect_scam_flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "Contact us on WhatsApp to apply.",
        "Message our recruiter via Telegram.",
        "Apply through telegram @easyjobs24",
        "Join t.me/ai_jobs_now today",
        "Text me on Signal for details",
    ],
)
def test_messaging_only_contact_is_flagged(text: str) -> None:
    assert scam.FLAG_MESSAGING_ONLY in scam.detect_scam_flags(text)


@pytest.mark.parametrize(
    "text",
    [
        "We are hiring a data annotator. Apply through our careers page.",
        "Pay is $25/hr, paid weekly by bank transfer after onboarding.",
        "You will review AI responses and write feedback. Fluent English required.",
        "The registration for the exam closes on Friday.",
        "Upload your CV and a short cover letter.",
    ],
)
def test_ordinary_postings_have_no_flags(text: str) -> None:
    assert scam.detect_scam_flags(text) == []


def test_extreme_pay_is_flagged_by_period() -> None:
    assert scam.FLAG_PAY_HIGH in scam.detect_scam_flags(
        "ok", pay_max=Decimal(900), pay_currency="USD", pay_period="hour"
    )
    assert scam.FLAG_PAY_HIGH in scam.detect_scam_flags(
        "ok", pay_max=Decimal(60000), pay_currency="USD", pay_period="month"
    )


def test_expert_level_pay_is_not_flagged() -> None:
    # ExpertWoka lists "$100 - $200/hr" for finance experts; that must not warn.
    assert (
        scam.detect_scam_flags("ok", pay_max=Decimal(200), pay_currency="USD", pay_period="hour")
        == []
    )


def test_pay_flag_needs_usd_and_a_known_period() -> None:
    assert (
        scam.detect_scam_flags("ok", pay_max=Decimal(9999), pay_currency="KES", pay_period="hour")
        == []
    )
    assert (
        scam.detect_scam_flags("ok", pay_max=Decimal(9999), pay_currency="USD", pay_period="year")
        == []
    )
    assert scam.detect_scam_flags("ok", pay_max=None, pay_currency="USD", pay_period="hour") == []


def test_multiple_flags_combine() -> None:
    text = "Pay a registration fee, then WhatsApp us on +254 700."
    flags = scam.detect_scam_flags(text)
    assert scam.FLAG_ASKS_PAYMENT in flags and scam.FLAG_MESSAGING_ONLY in flags


def test_every_flag_has_a_label() -> None:
    for code in (
        scam.FLAG_ASKS_PAYMENT,
        scam.FLAG_ASKS_ID_EARLY,
        scam.FLAG_MESSAGING_ONLY,
        scam.FLAG_PAY_HIGH,
    ):
        assert scam.FLAG_LABELS[code]
