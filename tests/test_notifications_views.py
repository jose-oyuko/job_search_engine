"""Settings → Notifications, onboarding, the hooks that start notifications, the real gateway
(against a mocked Bot API), logging hygiene and isolation (spec §13, §15.6, §16)."""

import json
import logging
from typing import Any

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client

from apps.accounts.models import UserSettings
from apps.kits.models import Kit
from apps.notifications import services
from apps.notifications.exceptions import GatewayError
from apps.notifications.gateway import Button, PtbGateway
from apps.notifications.models import Notification, TelegramLink, TelegramLinkToken
from tests.factories import UserFactory
from tests.kit_helpers import make_kit_job
from tests.notif_helpers import CHAT, make_notif_user, scored_match

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
SETTINGS_URL = "/app/settings/notifications/"
VALID = {
    "notify_mode": "daily",
    "digest_time": "07:30",
    "instant_score_threshold": "80",
    "auto_kit_threshold": "90",
    "notify_email": "on",
    "send_documents_telegram": "on",
}


@pytest.fixture
def me(tg) -> tuple[Client, Any]:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    client = Client()
    client.force_login(user)
    return client, user


# --- the settings page ------------------------------------------------------------------------------------


def test_the_page_shows_the_current_settings_and_a_connect_button(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    html = client.get(SETTINGS_URL).content.decode()
    assert "Notifications" in html and 'name="notify_mode"' in html and "Connect Telegram" in html
    assert (
        'name="digest_time"' in html
        and 'type="time"' in html
        and 'name="quiet_hours_start"' in html
    )


def test_the_settings_nav_links_to_the_page(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    assert SETTINGS_URL in client.get("/app/settings/account/").content.decode()


def test_saving_updates_the_users_settings(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    response = client.post(
        SETTINGS_URL,
        {**VALID, "quiet_hours_start": "22:00", "quiet_hours_end": "07:00"},
    )
    assert response.status_code == 302 and response["Location"] == SETTINGS_URL
    row = UserSettings.objects.get(user=user)
    assert (row.notify_mode, row.digest_time.strftime("%H:%M")) == ("daily", "07:30")
    assert (row.instant_score_threshold, row.auto_kit_threshold) == (80, 90)
    assert row.quiet_hours_start.hour == 22 and row.quiet_hours_end.hour == 7
    assert row.notify_email is True


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"notify_mode": "weekly"}, "notify_mode"),
        ({"digest_time": "25:99"}, "digest_time"),
        ({"instant_score_threshold": "101"}, "instant_score_threshold"),
        ({"auto_kit_threshold": "-1"}, "auto_kit_threshold"),
        ({"quiet_hours_start": "22:00"}, "quiet_hours_end"),
        ({"notify_telegram": "on"}, "notify_telegram"),  # not connected yet
    ],
)
def test_bad_values_are_rejected_and_nothing_is_saved(me, change, field) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    before = UserSettings.objects.values().get(user=user)
    response = client.post(SETTINGS_URL, {**VALID, **change})
    assert response.status_code == 200 and field in response.context["form"].errors
    assert UserSettings.objects.values().get(user=user) == before


def test_telegram_can_be_turned_on_once_connected(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    TelegramLink.objects.create(user=user, chat_id=CHAT)
    response = client.post(SETTINGS_URL, {**VALID, "notify_telegram": "on"})
    assert response.status_code == 302 and UserSettings.objects.get(user=user).notify_telegram


def test_connecting_shows_a_link_that_opens_the_bot(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    html = client.post("/app/settings/notifications/telegram/connect/").content.decode()
    token = TelegramLinkToken.objects.get(user=user)
    assert f"https://t.me/jobpilot_test_bot?start={token.token}" in html
    assert "15 minutes" in html


def test_connecting_needs_a_bot(me, settings) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    settings.TELEGRAM_BOT_USERNAME = ""
    response = client.post("/app/settings/notifications/telegram/connect/", follow=True)
    assert "not set up" in response.content.decode() and not TelegramLinkToken.objects.exists()


def test_a_connected_page_offers_disconnect_and_disconnect_works(me) -> None:  # type: ignore[no-untyped-def]
    client, user = me
    TelegramLink.objects.create(user=user, chat_id=CHAT, username="jane")
    UserSettings.objects.filter(user=user).update(notify_telegram=True)
    html = client.get(SETTINGS_URL).content.decode()
    assert "Connected as @jane" in html and "Disconnect Telegram" in html
    assert client.post("/app/settings/notifications/telegram/disconnect/").status_code == 302
    assert not TelegramLink.objects.exists()
    assert UserSettings.objects.get(user=user).notify_telegram is False


def test_connect_and_disconnect_only_accept_post_and_need_login(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    assert client.get("/app/settings/notifications/telegram/connect/").status_code == 405
    assert client.get("/app/settings/notifications/telegram/disconnect/").status_code == 405
    anonymous = Client()
    for url in (SETTINGS_URL, "/app/settings/notifications/telegram/connect/"):
        assert "/accounts/login/" in anonymous.get(url)["Location"]


def test_connecting_is_limited_to_ten_an_hour(me) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    codes = [
        client.post("/app/settings/notifications/telegram/connect/").status_code for _ in range(11)
    ]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_the_page_says_so_when_telegram_is_not_set_up(me, settings) -> None:  # type: ignore[no-untyped-def]
    client, _ = me
    settings.TELEGRAM_GATEWAY = "ptb"
    settings.TELEGRAM_BOT_TOKEN = ""
    assert "not set up on this server yet" in client.get(SETTINGS_URL).content.decode()


# --- onboarding step 5 ---------------------------------------------------------------------------------------


def test_the_onboarding_step_saves_mode_time_and_email(tg) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UserSettings.objects.create(user=user)
    client = Client()
    client.force_login(user)
    page = client.get("/welcome/notifications/").content.decode()
    assert "Step 5 of 5" in page and "Connect Telegram" in page
    response = client.post(
        "/welcome/notifications/", {"notify_mode": "hourly", "digest_time": "09:00"}
    )
    assert response["Location"] == "/welcome/done/"
    row = UserSettings.objects.get(user=user)
    assert (row.notify_mode, row.notify_email) == ("hourly", False)  # an unticked box is off


def test_the_onboarding_step_can_start_a_telegram_link(tg) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UserSettings.objects.create(user=user)
    client = Client()
    client.force_login(user)
    html = client.post("/welcome/notifications/", {"connect_telegram": "1"}).content.decode()
    token = TelegramLinkToken.objects.get(user=user)
    assert token.token in html and "Open Telegram" in html
    assert UserSettings.objects.get(user=user).notify_mode == "instant"  # nothing else changed


def test_the_onboarding_step_rejects_a_bad_mode(tg) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    UserSettings.objects.create(user=user)
    client = Client()
    client.force_login(user)
    response = client.post(
        "/welcome/notifications/", {"notify_mode": "never", "digest_time": "09:00"}
    )
    assert response.status_code == 200 and response.context["form"].errors


# --- hooks: scoring starts the alert -------------------------------------------------------------------------------


def test_scoring_a_match_above_the_threshold_queues_the_alert_and_the_kit(  # type: ignore[no-untyped-def]
    tg, fake_llm
) -> None:
    from apps.matching import services as matching_services
    from tests.factories import MatchFactory

    user = make_notif_user(notify_email=False)
    match = MatchFactory(user=user, job=make_kit_job(external_id="hook"))
    fake_llm.script.append(
        json.dumps(
            {"score": 91, "verdict": "strong", "strengths": ["a"], "gaps": [], "summary": "Good."}
        )
    )
    assert matching_services.score_match(match.pk) == matching_services.SCORED
    assert Kit.objects.filter(user=user, job=match.job, auto_generated=True).exists()
    assert Notification.objects.filter(user=user, kind="instant_match", channel="telegram").exists()


def test_a_failing_alert_never_breaks_scoring(tg, fake_llm, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.matching import services as matching_services
    from tests.factories import MatchFactory

    user = make_notif_user()
    match = MatchFactory(user=user, job=make_kit_job(external_id="boom"))
    monkeypatch.setattr(services, "request_instant", lambda m: 1 / 0)
    fake_llm.script.append(
        json.dumps({"score": 91, "verdict": "strong", "strengths": [], "gaps": [], "summary": "x"})
    )
    assert matching_services.score_match(match.pk) == matching_services.SCORED
    match.refresh_from_db()
    assert match.score == 91


def test_a_failing_kit_ready_notice_never_breaks_the_kit(tg, fake_llm, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from apps.kits import services as kit_services
    from tests.kit_helpers import good_cv, script_kit
    from tests.test_notifications_services import _ids_of

    user = make_notif_user()
    monkeypatch.setattr(services, "on_kit_ready", lambda k: 1 / 0)
    job = make_kit_job(external_id="kboom")
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit = kit_services.request_kit(user, job)
    kit_services.generate_kit(kit.pk)
    kit.refresh_from_db()
    assert kit.status == Kit.Status.READY


def test_a_source_going_failing_notifies_staff(tg, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    """The health-change branch of a source run queues one notice for linked staff."""
    import datetime

    from apps.sources import services as source_services
    from tests.factories import SourceFactory

    staff = make_notif_user(chat_id="42", full_name="Owner")
    staff.is_staff = True
    staff.save()
    source = SourceFactory(display_name="Flaky", health="failing", consecutive_failures=3)
    source_services._notify_staff_failing(source.pk, datetime.date(2026, 3, 4))
    assert Notification.objects.filter(user=staff, kind="source_health").count() == 1


def test_a_waitlist_request_notifies_staff_after_commit(
    tg, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts.services import submit_waitlist

    staff = make_notif_user(chat_id="42", full_name="Owner")
    staff.is_staff = True
    staff.save()
    with django_capture_on_commit_callbacks(execute=True):
        submit_waitlist(name="Wanjiku", email="w@example.com", reason="Work")
    assert Notification.objects.filter(user=staff, kind="system").count() == 1
    assert "Wanjiku" in tg.messages[0]["text"]


# --- the real gateway, against a mocked Bot API ---------------------------------------------------------------------


TOKEN = "123456:TEST-TOKEN-NOT-REAL"
API = f"https://api.telegram.org/bot{TOKEN}"
ME = {"ok": True, "result": {"id": 1, "is_bot": True, "first_name": "B", "username": "b_bot"}}


def test_the_ptb_gateway_sends_messages_with_buttons(router) -> None:  # type: ignore[no-untyped-def]
    router.post(f"{API}/getMe").mock(return_value=httpx.Response(200, json=ME))
    sent = router.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 5, "date": 1, "chat": {"id": 1, "type": "private"}},
            },
        )
    )
    PtbGateway(TOKEN).send_message(
        "555",
        "hello",
        buttons=[[Button("Open", url="https://example.com/x"), Button("Kit", data="k.1.abc")]],
    )
    from urllib.parse import unquote_plus

    body = unquote_plus(sent.calls.last.request.content.decode())
    assert "text=hello" in body and "https://example.com/x" in body and "k.1.abc" in body


def test_the_ptb_gateway_sends_documents(router) -> None:  # type: ignore[no-untyped-def]
    router.post(f"{API}/getMe").mock(return_value=httpx.Response(200, json=ME))
    sent = router.post(f"{API}/sendDocument").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 6, "date": 1, "chat": {"id": 1, "type": "private"}},
            },
        )
    )
    PtbGateway(TOKEN).send_document("555", "CV.pdf", b"%PDF-1.7 data", caption="Your CV")
    assert (
        b"CV.pdf" in sent.calls.last.request.content
        and b"%PDF-1.7 data" in sent.calls.last.request.content
    )


def test_a_blocked_bot_is_reported_as_blocked(router) -> None:  # type: ignore[no-untyped-def]
    router.post(f"{API}/getMe").mock(return_value=httpx.Response(200, json=ME))
    router.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            403,
            json={
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            },
        )
    )
    with pytest.raises(GatewayError) as excinfo:
        PtbGateway(TOKEN).send_message("555", "hi")
    assert excinfo.value.blocked is True and "blocked by the user" not in str(excinfo.value)


def test_other_telegram_errors_are_not_treated_as_blocked(router) -> None:  # type: ignore[no-untyped-def]
    router.post(f"{API}/getMe").mock(return_value=httpx.Response(200, json=ME))
    router.post(f"{API}/sendMessage").mock(
        return_value=httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )
    )
    with pytest.raises(GatewayError) as excinfo:
        PtbGateway(TOKEN).send_message("555", "hi")
    assert excinfo.value.blocked is False and excinfo.value.reason == "BadRequest"


def test_answering_a_callback_calls_the_bot_api(router) -> None:  # type: ignore[no-untyped-def]
    router.post(f"{API}/getMe").mock(return_value=httpx.Response(200, json=ME))
    answered = router.post(f"{API}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )
    PtbGateway(TOKEN).answer_callback("cb-1", "Done")
    assert b"cb-1" in answered.calls.last.request.content


def test_the_bot_application_registers_its_handlers() -> None:
    from apps.notifications.bot import build_application

    application = build_application(TOKEN)
    assert sum(len(group) for group in application.handlers.values()) == 2


def test_run_bot_needs_a_token(settings) -> None:  # type: ignore[no-untyped-def]
    settings.TELEGRAM_BOT_TOKEN = ""
    with pytest.raises(CommandError, match="TELEGRAM_BOT_TOKEN"):
        call_command("run_bot")


def test_the_gateway_choice_follows_the_settings(settings) -> None:  # type: ignore[no-untyped-def]
    from apps.notifications.gateway import get_gateway, telegram_enabled

    settings.TELEGRAM_GATEWAY = "ptb"
    settings.TELEGRAM_BOT_TOKEN = ""
    assert get_gateway() is None and not telegram_enabled()
    settings.TELEGRAM_BOT_TOKEN = TOKEN
    assert isinstance(get_gateway(), PtbGateway) and telegram_enabled()


# --- logging hygiene -------------------------------------------------------------------------------------------------


def test_nothing_personal_reaches_the_logs(tg, caplog) -> None:  # type: ignore[no-untyped-def]
    from apps.notifications import bot_services

    caplog.set_level(logging.DEBUG)
    user = make_notif_user()
    token = services.create_link_token(make_notif_user(chat_id=None, full_name="Second Person"))
    bot_services.handle_text("31337", "someone", f"/start {token.token}")
    match = scored_match(
        user, score=92, job=make_kit_job(external_id="logs", title="Secret Role Title")
    )
    for notification in services.request_instant(match):
        services.deliver(notification.pk)
    bot_services.handle_text(CHAT, "jane", "/status")
    bot_services.handle_callback("wrong-chat", "cb", "a.1.deadbeef")
    logged = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    for secret in (
        CHAT, "31337", user.email, "Secret Role Title", "Helpdesk experience",
        "Jane Wanjiru", token.token, "wrong-chat",
    ):  # fmt: skip
        assert secret not in logged, secret
    events = {getattr(r, "event", "") for r in caplog.records}
    assert {"telegram_linked", "notification_sent"} <= events


# --- isolation -------------------------------------------------------------------------------------------------------


def test_each_user_only_ever_sees_their_own_settings(tg) -> None:  # type: ignore[no-untyped-def]
    a, b = make_notif_user(chat_id=None), make_notif_user(chat_id="2", full_name="Bob Buffalo")
    UserSettings.objects.filter(user=b).update(notify_mode="off", digest_time="05:00")
    client = Client()
    client.force_login(a)
    response = client.post(SETTINGS_URL, {**VALID, "notify_mode": "hourly"})
    assert response.status_code == 302
    assert UserSettings.objects.get(user=b).notify_mode == "off"
    assert UserSettings.objects.get(user=a).notify_mode == "hourly"


def test_a_link_token_only_links_its_own_user(tg) -> None:  # type: ignore[no-untyped-def]
    a = make_notif_user(chat_id=None)
    b = make_notif_user(chat_id=None, full_name="Bob Buffalo")
    token_a = services.create_link_token(a)
    services.create_link_token(b)
    services.consume_link_token(token_a.token, "111")
    assert TelegramLink.objects.get(chat_id="111").user == a
    assert not TelegramLink.objects.filter(user=b).exists()


def test_for_user_scopes_notifications_and_tokens(tg) -> None:  # type: ignore[no-untyped-def]
    a, b = make_notif_user(), make_notif_user(chat_id="2", full_name="Bob Buffalo")
    services.request_instant(scored_match(a, score=90, job=make_kit_job(external_id="1")))
    services.request_instant(scored_match(b, score=90, job=make_kit_job(external_id="2")))
    services.create_link_token(b)
    assert Notification.objects.for_user(a).count() == 2
    assert all(n.user == a for n in Notification.objects.for_user(a))
    assert TelegramLinkToken.objects.for_user(a).count() == 0
    assert Notification.objects.for_user(None).count() == 0


def test_one_users_unlink_does_not_touch_anothers(tg) -> None:  # type: ignore[no-untyped-def]
    a, b = make_notif_user(), make_notif_user(chat_id="2", full_name="Bob Buffalo")
    services.unlink(a)
    assert TelegramLink.objects.filter(user=b).exists()
    assert UserSettings.objects.get(user=b).notify_telegram is True


def test_the_admin_shows_no_chat_ids_or_message_data(tg, admin_client) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    notification = services.request_instant(scored_match(user, score=90))[0]
    page = admin_client.get(
        f"/admin/notifications/notification/{notification.pk}/change/"
    ).content.decode()
    link = admin_client.get(
        f"/admin/notifications/telegramlink/{TelegramLink.objects.get().pk}/change/"
    )
    assert CHAT not in page and CHAT not in link.content.decode()
    assert "payload" not in page and "chat_id" not in link.content.decode()
