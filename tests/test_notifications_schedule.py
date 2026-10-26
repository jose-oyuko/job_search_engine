"""Quiet hours, digest due times and callback signatures (spec §13.2, §13.3, §13.1)."""

import datetime

import pytest

from apps.notifications import schedule, signing
from tests.notif_helpers import utc

T = datetime.time

# --- quiet hours --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "start", "end", "expected"),
    [
        (T(23, 0), T(22, 0), T(7, 0), True),  # overnight window, late evening
        (T(3, 0), T(22, 0), T(7, 0), True),  # overnight window, small hours
        (T(7, 0), T(22, 0), T(7, 0), False),  # the end is exclusive
        (T(22, 0), T(22, 0), T(7, 0), True),  # the start is inclusive
        (T(12, 0), T(22, 0), T(7, 0), False),
        (T(13, 0), T(12, 0), T(14, 0), True),  # same-day window
        (T(14, 0), T(12, 0), T(14, 0), False),
        (T(11, 59), T(12, 0), T(14, 0), False),
        (T(3, 0), None, None, False),  # not set
        (T(3, 0), T(22, 0), None, False),  # half set
        (T(3, 0), T(8, 0), T(8, 0), False),  # empty window
    ],
)
def test_in_quiet_hours(now, start, end, expected) -> None:  # type: ignore[no-untyped-def]
    assert schedule.in_quiet_hours(now, start, end) is expected


def test_quiet_hours_end_is_reported_in_utc_for_the_users_zone() -> None:
    # 23:30 in Nairobi (UTC+3) is 20:30 UTC; quiet hours 22:00-07:00 end at 07:00 local = 04:00 UTC
    now = utc(2026, 3, 4, 20, 30)
    assert schedule.quiet_hours_end(now, "Africa/Nairobi", T(22, 0), T(7, 0)) == utc(
        2026, 3, 5, 4, 0
    )


def test_quiet_hours_end_when_already_past_midnight() -> None:
    now = utc(2026, 3, 5, 0, 30)  # 03:30 in Nairobi
    assert schedule.quiet_hours_end(now, "Africa/Nairobi", T(22, 0), T(7, 0)) == utc(
        2026, 3, 5, 4, 0
    )


def test_no_end_when_not_in_quiet_hours() -> None:
    assert (
        schedule.quiet_hours_end(utc(2026, 3, 4, 9, 0), "Africa/Nairobi", T(22, 0), T(7, 0)) is None
    )
    assert schedule.quiet_hours_end(utc(2026, 3, 4, 9, 0), "Africa/Nairobi", None, None) is None


def test_unknown_zone_names_fall_back_to_nairobi() -> None:
    assert str(schedule.zone("Not/AZone")) == "Africa/Nairobi"
    assert str(schedule.zone("")) == "Africa/Nairobi"


# --- digest due times ----------------------------------------------------------------------------


EIGHT = T(8, 0)


def due(mode, now, last=None, tz="Africa/Nairobi", at=EIGHT, qs=None, qe=None):  # type: ignore[no-untyped-def]
    return schedule.digest_period(mode, now, tz, at, last, qs, qe)


def test_a_daily_digest_is_not_due_before_the_digest_time() -> None:
    assert due("daily", utc(2026, 3, 4, 4, 59)) is None  # 07:59 in Nairobi


def test_a_daily_digest_is_due_at_the_digest_time_in_the_users_zone() -> None:
    assert due("daily", utc(2026, 3, 4, 5, 0)) == "d:2026-03-04"  # 08:00 in Nairobi
    assert due("daily", utc(2026, 3, 4, 17, 0)) == "d:2026-03-04"  # still today's, if not yet sent


def test_a_daily_digest_is_not_due_again_once_sent_today() -> None:
    sent = utc(2026, 3, 4, 5, 2)
    assert due("daily", utc(2026, 3, 4, 5, 7), last=sent) is None
    assert due("daily", utc(2026, 3, 4, 20, 0), last=sent) is None


def test_the_next_days_digest_is_due_the_next_morning() -> None:
    sent = utc(2026, 3, 4, 5, 2)
    assert due("daily", utc(2026, 3, 5, 4, 59), last=sent) is None
    assert due("daily", utc(2026, 3, 5, 5, 0), last=sent) == "d:2026-03-05"


@pytest.mark.parametrize(
    ("tz", "utc_hour"),
    [
        ("Africa/Nairobi", 5),
        ("Africa/Kampala", 5),
        ("Africa/Lagos", 7),
        ("UTC", 8),
        ("Asia/Kolkata", 2),
    ],
)
def test_due_times_in_dst_free_zones(tz: str, utc_hour: int) -> None:
    minute = 30 if tz == "Asia/Kolkata" else 0
    before = utc(
        2026, 7, 1, utc_hour - 1 if tz != "Asia/Kolkata" else 2, 29 if tz == "Asia/Kolkata" else 59
    )
    on_time = utc(2026, 7, 1, utc_hour, minute)
    assert due("daily", before, tz=tz) is None
    assert due("daily", on_time, tz=tz) is not None


def test_the_local_date_decides_the_period_key() -> None:
    # 22:00 UTC on the 4th is already 01:00 on the 5th in Nairobi: the 5th's digest is not due yet
    assert due("daily", utc(2026, 3, 4, 22, 0), last=utc(2026, 3, 3, 5, 0)) is None
    assert due("daily", utc(2026, 3, 5, 5, 0), last=utc(2026, 3, 4, 5, 0)) == "d:2026-03-05"


def test_instant_mode_has_a_daily_digest_for_everything_else() -> None:
    assert due("instant", utc(2026, 3, 4, 5, 0)) == "d:2026-03-04"
    assert due("instant", utc(2026, 3, 4, 5, 0), last=utc(2026, 3, 4, 5, 1)) is None


def test_off_never_sends() -> None:
    assert due("off", utc(2026, 3, 4, 12, 0)) is None


def test_an_hourly_digest_waits_an_hour_between_sends() -> None:
    last = utc(2026, 3, 4, 9, 0)
    assert due("hourly", utc(2026, 3, 4, 9, 59), last=last) is None
    assert due("hourly", utc(2026, 3, 4, 10, 0), last=last) == "h:2026-03-04T13"  # local hour
    assert due("hourly", utc(2026, 3, 4, 10, 0)) == "h:2026-03-04T13"  # never sent: due


def test_an_hourly_digest_is_held_back_during_quiet_hours_and_released_after() -> None:
    kwargs = {"qs": T(22, 0), "qe": T(7, 0)}
    assert due("hourly", utc(2026, 3, 4, 20, 30), **kwargs) is None  # 23:30 in Nairobi
    assert due("hourly", utc(2026, 3, 5, 4, 0), **kwargs) == "h:2026-03-05T07"


def test_the_daily_digest_ignores_quiet_hours_because_the_user_chose_its_time() -> None:
    assert due("daily", utc(2026, 3, 4, 5, 0), qs=T(6, 0), qe=T(9, 0)) == "d:2026-03-04"


# --- callback signatures -------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["k", "a", "n"])
def test_a_signed_callback_verifies_for_its_user(action: str) -> None:
    data = signing.sign_callback(action, 42, 7)
    assert signing.verify_callback(data, 7) == (signing.ACTIONS[action], 42)


def test_callback_data_fits_telegrams_64_byte_limit() -> None:
    assert len(signing.sign_callback("k", 2_000_000_000, 2_000_000_000).encode()) <= 64


def test_another_user_cannot_use_the_callback() -> None:
    data = signing.sign_callback("a", 42, 7)
    assert signing.verify_callback(data, 8) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.replace(".42.", ".43."),  # another match
        lambda d: "n" + d[1:],  # another action
        lambda d: d[:-1] + ("0" if d[-1] != "0" else "1"),  # a damaged signature
        lambda d: d.rsplit(".", 1)[0],  # no signature
        lambda d: d + ".x",
        lambda d: "",
        lambda d: "k.abc.def",
        lambda d: "z.42." + d.rsplit(".", 1)[1],  # unknown action
    ],
)
def test_forged_callbacks_are_rejected(mutate) -> None:  # type: ignore[no-untyped-def]
    assert signing.verify_callback(mutate(signing.sign_callback("a", 42, 7)), 7) is None


def test_unknown_actions_cannot_be_signed() -> None:
    with pytest.raises(ValueError):
        signing.sign_callback("x", 1, 1)


def test_the_chat_hash_is_stable_short_and_hides_the_id() -> None:
    first = signing.chat_hash("123456789")
    assert first == signing.chat_hash("123456789") and first != signing.chat_hash("123456780")
    assert "123456789" not in first and len(first) == 10
