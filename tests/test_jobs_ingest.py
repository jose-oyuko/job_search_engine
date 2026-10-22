"""Ingestion: upsert, sanitising, normalisation, dedupe, expiry and LLM extraction (spec §7)."""

import datetime
from decimal import Decimal

import pytest
import time_machine
from django.utils import timezone

from apps.jobs import services
from apps.jobs.models import Job, JobReport
from apps.llm.exceptions import LLMOutputError
from apps.llm.models import LLMCache, LLMCall
from apps.llm.providers.fake import FakeProvider
from apps.sources.base import RawJob
from apps.sources.models import Source
from tests.factories import JobFactory, SourceFactory, UserFactory

pytestmark = pytest.mark.django_db
UTC = datetime.UTC


def raw(source: Source, external_id: str = "j1", **overrides: object) -> RawJob:
    fields: dict[str, object] = {
        "source": source.name,
        "external_id": external_id,
        "url": f"https://site.test/jobs/{external_id}",
        "title": "Data Annotator",
        "company": "LabelCo",
        "description_html": "<p>Label things.</p>",
        "pay_text": "$20/hr",
        "locations": ["Global"],
        "work_type": "Remote",
    }
    fields.update(overrides)
    return RawJob(**fields)  # type: ignore[arg-type]


@pytest.fixture
def source() -> Source:
    return SourceFactory(name="s1", display_name="Source One")


# --- creating and updating -------------------------------------------------------------------------------


def test_new_jobs_are_created_with_normalised_fields(source: Source) -> None:
    result = services.ingest_raw_jobs(source, [raw(source)])
    job = Job.objects.get()
    assert (result.new, result.updated, result.duplicates, result.errors) == (1, 0, 0, [])
    assert result.new_job_ids == [job.pk]
    assert (job.source, job.external_id, job.origin, job.visibility) == (
        source,
        "j1",
        "scraped",
        "public",
    )
    assert (job.pay_min, job.pay_max, job.pay_currency, job.pay_period) == (
        Decimal(20),
        Decimal(20),
        "USD",
        "hour",
    )
    assert job.is_global and job.eligibility_known and job.work_type == "remote"
    assert job.description_text == "Label things." and job.status == "active"
    assert job.dedupe_key == "labelco:data-annotator"
    assert job.first_seen_at == job.last_seen_at


def test_description_html_is_sanitised(source: Source) -> None:
    dirty = '<p onclick="x()">Hi <script>alert(1)</script><a href="javascript:evil()">bad</a> <a href="https://ok.test">ok</a></p><iframe src="//e"></iframe>'
    services.ingest_raw_jobs(source, [raw(source, description_html=dirty)])
    html = Job.objects.get().description_html
    for bad in ("<script", "onclick", "javascript:", "<iframe"):
        assert bad not in html
    assert "https://ok.test" in html and "Hi" in html


def test_text_is_derived_from_html_when_missing_and_kept_when_given(source: Source) -> None:
    services.ingest_raw_jobs(
        source,
        [
            raw(source, "a", description_html="<p>One</p><p>Two</p>", description_text=""),
            raw(source, "b", description_html="<p>Ignored</p>", description_text="Provided text"),
        ],
    )
    assert Job.objects.get(external_id="a").description_text == "One\nTwo"
    assert Job.objects.get(external_id="b").description_text == "Provided text"


def test_raw_payload_is_stored_as_plain_json(source: Source) -> None:
    payload = {"when": datetime.datetime(2026, 1, 1, tzinfo=UTC), "n": 1, "nested": {"set": {1}}}
    services.ingest_raw_jobs(source, [raw(source, raw=payload)])
    stored = Job.objects.get().raw
    assert (
        stored["n"] == 1
        and isinstance(stored["when"], str)
        and isinstance(stored["nested"]["set"], str)
    )


def test_invalid_urls_are_dropped_not_stored(source: Source) -> None:
    services.ingest_raw_jobs(
        source, [raw(source, url="javascript:alert(1)", apply_url="ftp://x/y")]
    )
    job = Job.objects.get()
    assert job.url == "" and job.apply_url == ""


def test_long_fields_are_clipped(source: Source) -> None:
    services.ingest_raw_jobs(
        source, [raw(source, title="T" * 400, company="C" * 400, pay_text="$" * 500)]
    )
    job = Job.objects.get()
    assert len(job.title) == 250 and len(job.company) == 200 and len(job.pay_text) == 300


def test_unparseable_pay_keeps_the_text_only(source: Source) -> None:
    services.ingest_raw_jobs(source, [raw(source, pay_text="Competitive")])
    job = Job.objects.get()
    assert job.pay_text == "Competitive" and job.pay_min is None and job.pay_period == "unknown"


def test_unknown_location_is_not_eligible_nor_excluded(source: Source) -> None:
    services.ingest_raw_jobs(source, [raw(source, locations=[])])
    job = Job.objects.get()
    assert job.eligibility_known is False and job.is_global is False


def test_scam_flags_are_recorded_but_the_job_is_kept(source: Source) -> None:
    text = "Pay a small registration fee and contact us on WhatsApp"
    services.ingest_raw_jobs(source, [raw(source, description_text=text, description_html="")])
    job = Job.objects.get()
    assert (
        set(job.scam_flags) == {"asks_for_payment", "messaging_app_contact"}
        and job.status == "active"
    )


def test_invalid_raw_jobs_are_reported_and_skipped(source: Source) -> None:
    result = services.ingest_raw_jobs(
        source,
        [
            raw(source, "ok"),
            raw(source, "", title="No id"),
            raw(source, "x", title="  "),
            raw(source, "y", company=""),
        ],
    )
    assert result.new == 1 and len(result.errors) == 3
    assert list(Job.objects.values_list("external_id", flat=True)) == ["ok"]


def test_rerunning_the_same_jobs_creates_no_duplicates(source: Source) -> None:
    batch = [raw(source, "a"), raw(source, "b")]
    first = services.ingest_raw_jobs(source, batch)
    second = services.ingest_raw_jobs(source, batch)
    assert (first.new, second.new, second.unchanged, second.updated) == (2, 0, 2, 0)
    assert Job.objects.count() == 2 and second.new_job_ids == []


def test_changed_fields_update_the_job_and_bump_last_seen(source: Source) -> None:
    with time_machine.travel(datetime.datetime(2026, 10, 1, tzinfo=UTC), tick=False):
        services.ingest_raw_jobs(source, [raw(source)])
    with time_machine.travel(datetime.datetime(2026, 10, 5, tzinfo=UTC), tick=False):
        result = services.ingest_raw_jobs(source, [raw(source, pay_text="$25/hr")])
    job = Job.objects.get()
    assert result.updated == 1 and job.pay_max == Decimal(25)
    assert job.first_seen_at.day == 1 and job.last_seen_at.day == 5


def test_a_changed_description_resets_extraction_and_is_reported(
    source: Source, fake_llm: FakeProvider
) -> None:
    services.ingest_raw_jobs(source, [raw(source)])
    job = Job.objects.get()
    assert services.extract_job_details(job.pk)
    result = services.ingest_raw_jobs(
        source, [raw(source, description_html="<p>Totally new text.</p>")]
    )
    job.refresh_from_db()
    assert result.changed_job_ids == [job.pk] and job.extracted is None and job.extracted_at is None


def test_values_the_source_omits_do_not_wipe_filled_ones(source: Source) -> None:
    services.ingest_raw_jobs(source, [raw(source, pay_text="", locations=[], work_type="")])
    job = Job.objects.get()
    Job.objects.filter(pk=job.pk).update(
        pay_text="$30/hr", pay_max=Decimal(30), work_type="hybrid", locations_raw=["Kenya"]
    )
    services.ingest_raw_jobs(source, [raw(source, pay_text="", locations=[], work_type="")])
    job.refresh_from_db()
    assert job.pay_text == "$30/hr" and job.work_type == "hybrid" and job.locations_raw == ["Kenya"]


def test_an_expired_job_that_reappears_becomes_active_again(source: Source) -> None:
    services.ingest_raw_jobs(source, [raw(source)])
    Job.objects.update(status="expired")
    services.ingest_raw_jobs(source, [raw(source)])
    assert Job.objects.get().status == "active"


def test_touch_seen_marks_listed_but_not_refetched_jobs(source: Source) -> None:
    old = timezone.now() - datetime.timedelta(days=5)
    a = JobFactory(source=source, external_id="a", last_seen_at=old, status="expired")
    b = JobFactory(source=source, external_id="b", last_seen_at=old)
    other = JobFactory(external_id="a", last_seen_at=old)  # same ID, different source
    assert services.touch_seen(source, {"a"}) == 1
    a.refresh_from_db()
    b.refresh_from_db()
    other.refresh_from_db()
    assert a.status == "active" and a.last_seen_at > old
    assert b.last_seen_at == old and other.last_seen_at == old
    assert services.touch_seen(source, set()) == 0


# --- dedupe ----------------------------------------------------------------------------------------------------


def test_dedupe_key_normalises_company_and_title() -> None:
    key = services.make_dedupe_key
    assert key("Micro1, Inc.", "Data Annotator (Remote)") == key("micro1", "data  annotator")
    assert key("Café Ltd", "Résumé Writer") == "cafe:resume-writer"
    assert key("A", "B") != key("A", "C")


def test_cross_source_duplicate_links_to_the_older_job() -> None:
    aggregator = SourceFactory(name="agg", display_name="Aggregator")
    platform = SourceFactory(name="micro1", display_name="micro1")
    services.ingest_raw_jobs(
        aggregator,
        [
            raw(
                aggregator,
                "g1",
                company="micro1",
                title="AI Rater",
                url="https://agg.test/1",
                apply_url="https://agg.test/apply/1",
            )
        ],
    )
    result = services.ingest_raw_jobs(
        platform,
        [
            raw(
                platform,
                "p9",
                company="micro1",
                title="AI Rater (Remote)",
                url="https://micro1.test/9",
                apply_url="https://micro1.test/apply/9",
            )
        ],
    )
    original = Job.objects.get(source=aggregator)
    duplicate = Job.objects.get(source=platform)
    original.refresh_from_db()
    assert duplicate.duplicate_of == original and original.duplicate_of is None
    assert (
        result.duplicates == 1 and result.new == 1 and result.new_job_ids == []
    )  # not extracted/notified
    assert original.alternate_urls == ["https://micro1.test/9"]
    assert original.apply_url == "https://micro1.test/apply/9"  # the platform's own link wins


def test_an_aggregator_duplicate_does_not_replace_a_platform_link() -> None:
    platform = SourceFactory(name="micro1", display_name="micro1")
    aggregator = SourceFactory(name="agg", display_name="Aggregator")
    services.ingest_raw_jobs(
        platform, [raw(platform, "p1", company="micro1", apply_url="https://micro1.test/apply")]
    )
    services.ingest_raw_jobs(
        aggregator,
        [
            raw(
                aggregator,
                "g1",
                company="micro1",
                url="https://agg.test/1",
                apply_url="https://agg.test/apply",
            )
        ],
    )
    original = Job.objects.get(source=platform)
    assert original.apply_url == "https://micro1.test/apply" and original.alternate_urls == [
        "https://agg.test/1"
    ]


def test_same_source_is_never_a_duplicate_and_old_or_expired_jobs_do_not_count() -> None:
    a, b = SourceFactory(name="a"), SourceFactory(name="b")
    services.ingest_raw_jobs(a, [raw(a, "1"), raw(a, "2")])  # same key, same source
    assert Job.objects.filter(duplicate_of__isnull=False).count() == 0

    old = timezone.now() - datetime.timedelta(days=31)
    Job.objects.update(first_seen_at=old)
    services.ingest_raw_jobs(b, [raw(b, "3")])  # the originals are older than 30 days
    assert Job.objects.get(source=b).duplicate_of is None

    Job.objects.update(first_seen_at=timezone.now(), status="expired")
    c = SourceFactory(name="c")
    services.ingest_raw_jobs(c, [raw(c, "4")])  # the originals are expired
    assert Job.objects.get(source=c).duplicate_of is None


def test_duplicates_of_the_same_job_all_point_at_the_original() -> None:
    sources = [SourceFactory(name=f"s{i}") for i in range(3)]
    for index, src in enumerate(sources):
        services.ingest_raw_jobs(src, [raw(src, f"x{index}", url=f"https://s{index}.test/1")])
    jobs = list(Job.objects.order_by("id"))
    assert jobs[0].duplicate_of is None
    assert jobs[1].duplicate_of == jobs[0] and jobs[2].duplicate_of == jobs[0]
    jobs[0].refresh_from_db()
    assert jobs[0].alternate_urls == ["https://s1.test/1", "https://s2.test/1"]


# --- expiry -----------------------------------------------------------------------------------------------------


def test_scraped_jobs_expire_after_the_configured_days(settings) -> None:  # type: ignore[no-untyped-def]
    settings.JOB_EXPIRY_DAYS = 7
    src = SourceFactory()
    fresh = JobFactory(source=src, last_seen_at=timezone.now() - datetime.timedelta(days=6))
    stale = JobFactory(source=src, last_seen_at=timezone.now() - datetime.timedelta(days=8))
    assert services.expire_by_age() == 1
    fresh.refresh_from_db()
    stale.refresh_from_db()
    assert (fresh.status, stale.status) == ("active", "expired")
    assert services.expire_by_age() == 0  # idempotent


def test_a_source_can_set_its_own_expiry_window() -> None:
    src = SourceFactory(config={"expire_after_days": 30})
    job = JobFactory(source=src, last_seen_at=timezone.now() - datetime.timedelta(days=20))
    other = JobFactory(last_seen_at=timezone.now() - datetime.timedelta(days=20))
    services.expire_by_age()
    job.refresh_from_db()
    other.refresh_from_db()
    assert (job.status, other.status) == ("active", "expired")


def test_manual_and_email_jobs_expire_after_45_days() -> None:
    user = UserFactory()
    young = JobFactory(
        source=None,
        origin="manual_text",
        owner=user,
        visibility="private",
        last_seen_at=timezone.now() - datetime.timedelta(days=44),
    )
    old = JobFactory(
        source=None, origin="email", last_seen_at=timezone.now() - datetime.timedelta(days=46)
    )
    services.expire_by_age()
    young.refresh_from_db()
    old.refresh_from_db()
    assert (young.status, old.status) == ("active", "expired")


def test_expiry_time_machine_boundary(settings) -> None:  # type: ignore[no-untyped-def]
    settings.JOB_EXPIRY_DAYS = 7
    src = SourceFactory()
    with time_machine.travel(datetime.datetime(2026, 10, 1, tzinfo=UTC), tick=False):
        job = JobFactory(source=src)
    now = datetime.datetime(2026, 10, 8, 0, 0, 1, tzinfo=UTC)
    assert services.expire_by_age(now) == 1
    job.refresh_from_db()
    assert job.status == "expired"


# --- visibility, model constraints -------------------------------------------------------------------------------


def test_visible_to_shows_public_jobs_and_the_owners_private_ones() -> None:
    a, b = UserFactory(), UserFactory()
    public = JobFactory()
    mine = JobFactory(source=None, visibility="private", owner=a, origin="manual_text")
    theirs = JobFactory(source=None, visibility="private", owner=b, origin="manual_text")
    visible = set(Job.objects.visible_to(a))
    assert visible == {public, mine} and theirs not in visible
    assert Job.objects.visible_to(None).count() == 0


def test_source_and_external_id_are_unique_but_manual_jobs_may_share_blanks() -> None:
    from django.db import IntegrityError, transaction

    src = SourceFactory()
    JobFactory(source=src, external_id="same")
    with pytest.raises(IntegrityError), transaction.atomic():
        JobFactory(source=src, external_id="same")
    JobFactory(source=None, external_id="")
    JobFactory(source=None, external_id="")  # no constraint without a source


def test_canonical_and_active_querysets() -> None:
    original = JobFactory()
    JobFactory(duplicate_of=original)
    JobFactory(status="expired")
    assert list(Job.objects.canonical().active()) == [original]


def test_job_reports_are_scoped_to_their_user() -> None:
    a, b = UserFactory(), UserFactory()
    job = JobFactory()
    JobReport.objects.create(user=a, job=job, reason="scam")
    JobReport.objects.create(user=b, job=job, reason="expired")
    assert (
        JobReport.objects.for_user(a).count() == 1
        and JobReport.objects.for_user(a).get().reason == "scam"
    )


# --- LLM extraction ----------------------------------------------------------------------------------------------


@pytest.fixture
def job(source: Source) -> Job:
    services.ingest_raw_jobs(
        source,
        [
            raw(
                source,
                pay_text="",
                locations=[],
                work_type="",
                description_html="<p>Review AI answers 10 hours a week.</p>",
            )
        ],
    )
    return Job.objects.get()


def test_extraction_stores_the_result_and_fills_gaps(job: Job, fake_llm: FakeProvider) -> None:
    fake_llm.script.append(
        '{"summary": "Review AI answers.", "seniority": "junior", "required_skills": ["Writing"], '
        '"nice_to_have_skills": [], "responsibilities": ["Review"], "requirements": ["10 hours per week"], '
        '"hours_commitment": "10 hours per week", "pay_text": "$18 per hour", "locations": ["Kenya"], '
        '"work_type": "remote", "application_steps": [], "red_flags": []}'
    )
    assert services.extract_job_details(job.pk) is True
    job.refresh_from_db()
    assert job.extracted["seniority"] == "junior" and job.extracted_at is not None
    assert (job.pay_max, job.pay_currency, job.pay_period) == (Decimal(18), "USD", "hour")
    assert job.eligible_countries == ["KE"] and job.eligibility_known and job.work_type == "remote"
    call = LLMCall.objects.get()
    assert call.task == "extract_job" and call.user is None and call.success  # system-paid


def test_extraction_does_not_override_what_the_source_provided(
    source: Source, fake_llm: FakeProvider
) -> None:
    services.ingest_raw_jobs(source, [raw(source)])  # pay $20/hr, Global, remote
    services.extract_job_details(Job.objects.get().pk)  # fixture says remote, no pay, no locations
    job = Job.objects.get()
    assert job.pay_max == Decimal(20) and job.is_global and job.work_type == "remote"


def test_extraction_is_idempotent(job: Job, fake_llm: FakeProvider) -> None:
    assert services.extract_job_details(job.pk) and services.extract_job_details(job.pk)
    assert len(fake_llm.calls) == 1


def test_extraction_prompt_wraps_the_posting_as_untrusted(job: Job, fake_llm: FakeProvider) -> None:
    services.extract_job_details(job.pk)
    content = fake_llm.calls[0]["messages"][0]["content"]
    assert "<job_posting>" in content and "Review AI answers" in content


def test_red_flags_from_the_model_add_a_flag(job: Job, fake_llm: FakeProvider) -> None:
    fake_llm.script.append(
        '{"summary": "x", "seniority": "unknown", "required_skills": [], "nice_to_have_skills": [], '
        '"responsibilities": [], "requirements": [], "red_flags": ["asks for a fee"]}'
    )
    services.extract_job_details(job.pk)
    job.refresh_from_db()
    assert "llm_red_flag" in job.scam_flags


def test_lists_from_the_model_are_trimmed_to_the_documented_maximums(
    job: Job, fake_llm: FakeProvider
) -> None:
    import json

    fake_llm.script.append(
        json.dumps(
            {
                "summary": "x",
                "seniority": "mid",
                "required_skills": [f"s{i}" for i in range(30)],
                "nice_to_have_skills": [f"n{i}" for i in range(30)],
                "responsibilities": [f"r{i}" for i in range(30)],
                "requirements": [f"q{i}" for i in range(30)],
            }
        )
    )
    services.extract_job_details(job.pk)
    job.refresh_from_db()
    ex = job.extracted
    assert (
        len(ex["required_skills"]),
        len(ex["nice_to_have_skills"]),
        len(ex["responsibilities"]),
        len(ex["requirements"]),
    ) == (15, 10, 8, 8)


def test_duplicates_are_not_extracted(fake_llm: FakeProvider) -> None:
    original = JobFactory()
    duplicate = JobFactory(duplicate_of=original)
    assert services.extract_job_details(duplicate.pk) is False
    assert fake_llm.calls == []


def test_unknown_job_ids_are_ignored() -> None:
    assert services.extract_job_details(999_999) is False


def test_llm_failure_leaves_the_job_usable(job: Job, fake_llm: FakeProvider, handler) -> None:  # type: ignore[no-untyped-def]
    fake_llm.script.extend(["junk", "junk"])
    assert services.extract_job_details(job.pk) is False
    job.refresh_from_db()
    assert job.extracted_at is None and job.status == "active"
    assert handler.events("job_extraction_failed")


def test_system_cap_pauses_extraction_without_error(
    job: Job, fake_llm: FakeProvider, settings, handler
) -> None:  # type: ignore[no-untyped-def]
    settings.LLM_SYSTEM_DAILY_USD_CAP = "0.10"
    LLMCall.objects.create(
        user=None, task="extract_job", provider="x", model="m", cost_usd=Decimal("0.10")
    )
    assert services.extract_job_details(job.pk) is False
    assert fake_llm.calls == [] and handler.events("job_extraction_skipped")


def test_extraction_is_cached_by_content(job: Job, fake_llm: FakeProvider) -> None:
    services.extract_job_details(job.pk)
    Job.objects.filter(pk=job.pk).update(extracted=None, extracted_at=None)
    services.extract_job_details(job.pk)  # same text: answered from the cache
    assert len(fake_llm.calls) == 1 and LLMCache.objects.count() == 1
    assert LLMCall.objects.filter(cache_hit=True).count() == 1


def test_logs_never_contain_job_text(job: Job, fake_llm: FakeProvider, handler) -> None:  # type: ignore[no-untyped-def]
    services.extract_job_details(job.pk)
    services.ingest_raw_jobs(
        job.source, [raw(job.source, "z", title="Secret Title", company="Secret Co")]
    )
    output = "".join(handler.lines)
    assert (
        "Secret Title" not in output
        and "Secret Co" not in output
        and "Review AI answers" not in output
    )
    assert LLMOutputError  # imported for completeness of failure-path coverage
