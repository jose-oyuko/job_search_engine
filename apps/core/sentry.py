"""Optional Sentry setup (spec §21.1). Does nothing unless a DSN is configured."""

from typing import Any

import sentry_sdk

from apps.core.logging import _redact_value, mask_text


def scrub_event(event: Any, hint: Any = None) -> Any:
    """`before_send` hook: apply the same redaction rules as the log filter."""
    for key in ("request", "extra", "contexts", "user"):
        if key in event:
            event[key] = _redact_value(key, event[key])
    if "message" in event and isinstance(event["message"], str):
        event["message"] = mask_text(event["message"])
    return event


def init_sentry(
    *, dsn: str, environment: str, release: str, service: str, traces_sample_rate: float
) -> bool:
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=release,
        send_default_pii=False,
        traces_sample_rate=traces_sample_rate,
        before_send=scrub_event,
    )
    sentry_sdk.set_tag("service", service)
    return True
