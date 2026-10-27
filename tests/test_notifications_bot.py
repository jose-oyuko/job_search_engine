"""The bot's behaviour with the fake gateway: linking, commands, buttons (spec §13.1)."""

import datetime
import json
from typing import Any

import pytest
import time_machine

from apps.accounts.models import UserSettings
from apps.kits.models import Kit
from apps.matching.models import Match
from apps.notifications import bot_services, services, signing
from apps.notifications.models import Notification, TelegramLink
from tests.kit_helpers import good_cv, make_kit_job, script_kit
from tests.notif_helpers import CHAT, make_notif_user, scored_match
from tests.test_notifications_services import _ids_of

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
STRANGER = "999000"


def say(chat: str, text: str, username: str = "jane") -> None:
    bot_services.handle_text(chat, username, text)


def last(tg: Any) -> str:
    return tg.messages[-1]["text"]


def press(tg: Any, chat: str, data: str, callback: str = "cb1") -> str:
    bot_services.handle_callback(chat, callback, data)
    return str(tg.answers[-1]["text"])


# --- linking and strangers -------------------------------------------------------------------------


def test_start_with_a_valid_token_links_the_chat(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    token = services.create_link_token(user)
    say("777", f"/start {token.token}", username="janedoe")
    assert f"Connected to JobPilot as {user.email}" in last(tg)
    link = TelegramLink.objects.get(user=user)
    assert (link.chat_id, link.username) == ("777", "janedoe")
    assert UserSettings.objects.get(user=user).notify_telegram is True


def test_start_with_a_bad_or_expired_token_explains(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user(chat_id=None)
    say("777", "/start nonsense")
    assert "expired" in last(tg) and not TelegramLink.objects.exists()


def test_start_from_a_chat_linked_elsewhere_is_refused(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user(chat_id="777")
    other = make_notif_user(chat_id=None, full_name="Other Person")
    token = services.create_link_token(other)
    say("777", f"/start {token.token}")
    assert "already connected to another account" in last(tg)


@pytest.mark.parametrize(
    "text", ["/start", "hello", "/status", "/search", "/digest", "/pause", "/resume", "/help", "/x"]
)
def test_unknown_chats_only_get_the_invite_only_notice(tg, text: str) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    say(STRANGER, text)
    assert tg.messages == [{"chat_id": STRANGER, "text": bot_services.INVITE_ONLY, "buttons": None}]
    assert UserSettings.objects.get(user=user).notify_mode == "instant"
    assert not Notification.objects.exists()


def test_start_without_a_token_greets_a_linked_user(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    say(CHAT, "/start")
    assert "You are connected." in last(tg) and "/status" in last(tg)


def test_commands_addressed_to_the_bot_by_name_work(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    say(CHAT, "/help@jobpilot_test_bot")
    assert "/pause" in last(tg)


def test_other_text_gets_the_help_list(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    say(CHAT, "what can you do")
    assert "I did not understand that." in last(tg) and "/digest" in last(tg)


def test_an_inactive_link_is_treated_as_a_stranger(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    TelegramLink.objects.filter(user=user).update(active=False)
    say(CHAT, "/status")
    assert last(tg) == bot_services.INVITE_ONLY


# --- commands ------------------------------------------------------------------------------------------


def test_status_counts_todays_matches(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    scored_match(user, score=70, job=make_kit_job(external_id="s1"))
    scored_match(user, score=85, job=make_kit_job(external_id="s2"))
    scored_match(user, score=99, job=make_kit_job(external_id="s3"), rule_pass=False)
    say(CHAT, "/status")
    text = last(tg)
    assert "New matches today: 2" in text and "Not looked at yet: 2" in text
    assert "Best score today: 85" in text and "Alerts: instant" in text


def test_status_only_counts_the_users_own_matches(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    other = make_notif_user(chat_id="2", full_name="Other Person")
    scored_match(other, score=90, job=make_kit_job(external_id="o1"))
    say(CHAT, "/status")
    assert "New matches today: 0" in last(tg)


def test_pause_and_resume_restore_the_previous_mode(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="hourly")
    say(CHAT, "/pause")
    row = UserSettings.objects.get(user=user)
    assert (row.notify_mode, row.previous_notify_mode) == ("off", "hourly")
    assert "paused" in last(tg)
    say(CHAT, "/pause")
    assert "already paused" in last(tg)
    say(CHAT, "/resume")
    row.refresh_from_db()
    assert (row.notify_mode, row.previous_notify_mode) == ("hourly", "")
    assert "back on (hourly digest)" in last(tg)
    say(CHAT, "/resume")
    assert "already on" in last(tg)


def test_resume_defaults_to_instant_when_nothing_was_remembered(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="off")
    say(CHAT, "/resume")
    assert UserSettings.objects.get(user=user).notify_mode == "instant"


def test_pause_stops_instant_alerts(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    say(CHAT, "/pause")
    assert services.request_instant(scored_match(user, score=95)) == []


def test_search_starts_a_search_and_is_rate_limited(tg, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    with django_capture_on_commit_callbacks(execute=False):
        say(CHAT, "/search")
        assert "Looking for new jobs" in last(tg)
        say(CHAT, "/search")
    assert "searched a moment ago" in last(tg) and "min" in last(tg)


def test_digest_sends_one_now(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    scored_match(user, score=70, job=make_kit_job(external_id="dg", title="Digest Job"))
    say(CHAT, "/digest")
    assert "Digest Job" in last(tg) and last(tg).startswith("📬 1 new match")


def test_digest_says_when_there_is_nothing(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user(notify_email=False)
    say(CHAT, "/digest")
    assert last(tg) == "Nothing new to report right now."


def test_the_bot_goes_quiet_past_thirty_commands_a_minute(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    with time_machine.travel("2026-03-04 10:00:00+00:00", tick=False):
        for _ in range(35):
            say(CHAT, "/help")
        assert len(tg.messages) == 30
        say("another-chat", "/help")  # the limit is per chat
        assert len(tg.messages) == 31
    with time_machine.travel("2026-03-04 10:01:05+00:00", tick=False):
        say(CHAT, "/help")
    assert len(tg.messages) == 32


# --- buttons ---------------------------------------------------------------------------------------------------


@pytest.fixture
def alert(tg) -> tuple[Any, Any, dict[str, str]]:  # type: ignore[no-untyped-def]
    """A user with an instant alert delivered; returns the user, the match and its button data."""
    user = make_notif_user(notify_email=False)
    match = scored_match(user, score=92, job=make_kit_job(external_id="btn"))
    services.deliver(services.request_instant(match)[0].pk)
    buttons = {b.text: b.data for b in tg.messages[0]["buttons"][0] if b.data}
    tg.reset()
    return user, match, buttons


def test_applied_marks_the_match_applied(tg, alert) -> None:  # type: ignore[no-untyped-def]
    _, match, buttons = alert
    assert press(tg, CHAT, buttons["Applied ✓"]) == "Marked as applied ✓"
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED


def test_not_interested_dismisses_with_a_reason(tg, alert) -> None:  # type: ignore[no-untyped-def]
    _, match, buttons = alert
    assert "Dismissed" in press(tg, CHAT, buttons["Not interested"])
    match.refresh_from_db()
    assert match.status == Match.Status.DISMISSED and match.dismiss_reason == "not_relevant"


def test_the_kit_button_queues_a_kit_then_delivers_it(tg, alert, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user, match, buttons = alert
    assert "Preparing your kit" in press(tg, CHAT, buttons["Kit"])
    kit = Kit.objects.get(user=user)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    from apps.kits import services as kit_services

    kit_services.generate_kit(kit.pk)
    assert len(tg.documents) == 2 and "is ready" in last(tg)
    assert "Sending your CV" in press(tg, CHAT, buttons["Kit"], "cb2")  # now it is ready
    assert len(tg.documents) == 4


def test_the_kit_button_answers_with_the_limit(tg, alert) -> None:  # type: ignore[no-untyped-def]
    from apps.llm.models import UsageQuota

    user, _, buttons = alert
    UsageQuota.objects.create(user=user, monthly_kits=0)
    assert "used this month's application kits" in press(tg, CHAT, buttons["Kit"])


def test_each_action_is_reflected_on_the_dashboard(tg, alert, client) -> None:  # type: ignore[no-untyped-def]
    user, match, buttons = alert
    client.force_login(user)
    assert match.job.title in client.get("/app/").content.decode()
    press(tg, CHAT, buttons["Not interested"])
    assert match.job.title not in client.get("/app/").content.decode()
    assert match.job.title in client.get("/app/?status=dismissed").content.decode()


# --- callbacks that must be refused ------------------------------------------------------------------------------


def test_a_button_pressed_from_the_wrong_chat_is_rejected(tg, alert) -> None:  # type: ignore[no-untyped-def]
    _, match, buttons = alert
    intruder = make_notif_user(chat_id="2468", full_name="Intruder Person")
    assert press(tg, "2468", buttons["Applied ✓"]) == "This button is no longer valid."
    assert press(tg, STRANGER, buttons["Applied ✓"]) == "This button is no longer valid."
    match.refresh_from_db()
    assert match.status == Match.Status.NOTIFIED  # untouched
    assert intruder.pk != match.user_id


@pytest.mark.parametrize("data", ["", "garbage", "a.1.deadbeef0000", "a.99999.aaaaaaaaaaaa", "k.1"])
def test_forged_button_data_is_rejected(tg, alert, data: str) -> None:  # type: ignore[no-untyped-def]
    _, match, _ = alert
    assert press(tg, CHAT, data) == "This button is no longer valid."
    match.refresh_from_db()
    assert match.status == Match.Status.NOTIFIED


def test_a_valid_signature_for_another_users_match_does_not_reach_it(tg, alert) -> None:  # type: ignore[no-untyped-def]
    user, _, _ = alert
    other = make_notif_user(chat_id="2468", full_name="Other Person")
    theirs = scored_match(other, score=90, job=make_kit_job(external_id="theirs"))
    forged = signing.sign_callback("a", theirs.pk, user.pk)  # a signature for the wrong owner pair
    assert press(tg, CHAT, forged) == "This job is no longer available."
    theirs.refresh_from_db()
    assert theirs.status == Match.Status.NEW


def test_a_button_for_a_deleted_match_is_handled(tg, alert) -> None:  # type: ignore[no-untyped-def]
    _, match, buttons = alert
    Match.objects.filter(pk=match.pk).delete()
    assert press(tg, CHAT, buttons["Applied ✓"]) == "This job is no longer available."


def test_callbacks_from_unlinked_chats_leave_no_trace(tg, alert) -> None:  # type: ignore[no-untyped-def]
    _, match, buttons = alert
    before = json.dumps(list(Match.objects.values("status")))
    press(tg, STRANGER, buttons["Not interested"])
    assert json.dumps(list(Match.objects.values("status"))) == before


def test_the_bot_does_nothing_when_telegram_is_not_configured(settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_GATEWAY = "ptb"
    settings.TELEGRAM_BOT_TOKEN = ""
    bot_services.handle_text(CHAT, "x", "/help")  # no gateway: no error, nothing sent
    bot_services.handle_callback(CHAT, "cb", "k.1.abc")
    assert datetime.date.today()


def test_a_timed_out_toast_does_not_undo_or_crash_the_action(tg, alert) -> None:  # type: ignore[no-untyped-def]
    """The popup after a button press is cosmetic: the job is already marked applied."""
    from apps.notifications.exceptions import GatewayError
    from apps.notifications.gateway import FakeGateway

    _, match, buttons = alert

    def failing_answer(self: FakeGateway, callback_id: str, text: str = "") -> None:
        raise GatewayError("TimedOut")

    original = FakeGateway.answer_callback
    FakeGateway.answer_callback = failing_answer  # type: ignore[method-assign]
    try:
        bot_services.handle_callback(CHAT, "cb-x", buttons["Applied ✓"])  # must not raise
    finally:
        FakeGateway.answer_callback = original  # type: ignore[method-assign]
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED
