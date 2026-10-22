"""Pay parsing table tests (spec §7.2): every example from the spec, plus real ExpertWoka values."""

from decimal import Decimal

import pytest

from apps.jobs.pay import Pay, parse_pay

D = Decimal


@pytest.mark.parametrize(
    ("text", "minimum", "maximum", "currency", "period"),
    [
        # --- the spec's examples
        ("$50 - $80/hr", D(50), D(80), "USD", "hour"),
        ("Up to $23/hr", None, D(23), "USD", "hour"),
        ("$15 - $15/hr", D(15), D(15), "USD", "hour"),
        ("Max rate: $20/hr", None, D(20), "USD", "hour"),
        ("$60 per task", D(60), D(60), "USD", "task"),
        ("PPT Rate: $60 per task; AHT: 6 hours", D(60), D(60), "USD", "task"),
        ("$2,535 one-time lump sum", D("2535"), D("2535"), "USD", "one_time"),
        ("$300000-500000", D(300000), D(500000), "USD", "year"),
        ("KES 80,000 per month", D(80000), D(80000), "KES", "month"),
        ("Ksh 50k", D(50000), D(50000), "KES", "unknown"),
        ("USD 1,200/month", D(1200), D(1200), "USD", "month"),
        # --- values seen on the live site
        ("$65-$75 per task", D(65), D(75), "USD", "task"),
        ("$25/hr", D(25), D(25), "USD", "hour"),
        ("$100 - $200/hr", D(100), D(200), "USD", "hour"),
        ("$20-$28/hr", D(20), D(28), "USD", "hour"),
        ("$3.5/task", D("3.5"), D("3.5"), "USD", "task"),
        ("$1.50 USD per qualified message", D("1.50"), D("1.50"), "USD", "unknown"),
        # --- other wording
        ("$20 per hour", D(20), D(20), "USD", "hour"),
        ("$20.50 hourly", D("20.50"), D("20.50"), "USD", "hour"),
        ("$45 - 60 per hour", D(45), D(60), "USD", "hour"),
        ("$45 to $60 per hour", D(45), D(60), "USD", "hour"),
        ("from $30/hr", D(30), None, "USD", "hour"),
        ("Starting at $18 per hour", D(18), None, "USD", "hour"),
        ("At least $5,000 per month", D(5000), None, "USD", "month"),
        ("$25+/hr", D(25), None, "USD", "hour"),
        ("At most $30/hr", None, D(30), "USD", "hour"),
        ("€2,000 per month", D(2000), D(2000), "EUR", "month"),
        ("£35k per year", D(35000), D(35000), "GBP", "year"),
        ("$80k - $120k", D(80000), D(120000), "USD", "year"),
        ("$120,000 annually", D(120000), D(120000), "USD", "year"),
        ("$500 per week", D(500), D(500), "USD", "week"),
        ("$150 per day", D(150), D(150), "USD", "day"),
        ("$3,000 flat fee", D(3000), D(3000), "USD", "one_time"),
        ("Pay: $12/hour (paid weekly)", D(12), D(12), "USD", "hour"),
        ("ksh 1,500 per day", D(1500), D(1500), "KES", "day"),
        ("KES 120,000", D(120000), D(120000), "KES", "unknown"),
        ("NGN 250,000 monthly", D(250000), D(250000), "NGN", "month"),
        ("$80 - $40/hr", D(40), D(80), "USD", "hour"),  # reversed ranges are put in order
    ],
)
def test_pay_examples(
    text: str, minimum: Decimal | None, maximum: Decimal | None, currency: str, period: str
) -> None:
    assert parse_pay(text) == Pay(minimum, maximum, currency, period)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "Not specified",
        "not disclosed",
        "N/A",
        "Competitive",
        "Negotiable",
        "TBD",
        "Great benefits and a friendly team",
        "6 hours per week",
        "AHT: 6 hours",
        "Experience: 5 years",
        "Paid per qualified message",
    ],
)
def test_unparseable_text_stays_unknown(text: str | None) -> None:
    pay = parse_pay(text)
    assert pay == Pay() and not pay.parsed and pay.period == "unknown"


def test_a_number_without_a_currency_is_never_money() -> None:
    assert parse_pay("50 - 80 per hour") == Pay()


def test_annual_default_only_applies_to_strong_currencies_over_the_threshold() -> None:
    assert parse_pay("$10,000").period == "unknown"  # not over 10,000
    assert parse_pay("$10,001").period == "year"
    assert parse_pay("KES 500,000").period == "unknown"  # a month's pay in many cases
    assert parse_pay("$300000").period == "year"


def test_explicit_period_beats_the_annual_default() -> None:
    assert parse_pay("$20,000 per month").period == "month"


def test_parsed_property() -> None:
    assert parse_pay("$5/hr").parsed
    assert not Pay().parsed


def test_first_amount_wins_when_text_lists_several() -> None:
    pay = parse_pay("$40/hr for reviews, $60/hr for leads")
    assert (pay.minimum, pay.maximum, pay.period) == (D(40), D(40), "hour")
