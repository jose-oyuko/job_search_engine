from apps.core.sentry import init_sentry, scrub_event


def test_sentry_stays_off_without_dsn() -> None:
    assert (
        init_sentry(dsn="", environment="t", release="r", service="web", traces_sample_rate=0.0)
        is False
    )


def test_scrub_event_applies_redaction_rules() -> None:
    event = {
        "message": "failed for dave@example.com",
        "request": {"headers": {"Authorization": "Bearer abc", "Accept": "text/html"}},
        "extra": {"password": "hunter2", "job_id": 7},
    }
    scrubbed = scrub_event(event)
    assert "dave@example.com" not in scrubbed["message"]
    assert scrubbed["request"]["headers"]["Authorization"] == "[redacted]"
    assert scrubbed["request"]["headers"]["Accept"] == "text/html"
    assert scrubbed["extra"] == {"password": "[redacted]", "job_id": 7}
