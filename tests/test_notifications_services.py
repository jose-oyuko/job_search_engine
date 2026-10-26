"""Linking, instant alerts, digests, delivery, the dispatcher and staff notices (spec §13)."""

import datetime
import json
import smtplib
from typing import Any

import pytest
import time_machine
from django.core import mail
from django.utils import timezone

from apps.accounts.models import UserSettings
from apps.kits import services as kit_services
from apps.kits.models import Kit
from apps.matching.models import Match
from apps.notifications import services
from apps.notifications.exceptions import (
    ChatAlreadyLinked,
    GatewayError,
    LinkTokenInvalid,
    TelegramUnavailable,
)
from apps.notifications.models import Notification, TelegramLink, TelegramLinkToken
from tests.factories import UserFactory
from tests.kit_helpers import good_cv, make_kit_job, script_kit
from tests.notif_helpers import CHAT, make_notif_user, scored_match, utc

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
TG, EMAIL = Notification.Channel.TELEGRAM, Notification.Channel.EMAIL


def rows(**filters: Any) -> list[Notification]:
    return list(Notification.objects.filter(**filters).order_by("id"))


# --- linking -------------------------------------------------------------------------------------


def test_a_link_token_lasts_fifteen_minutes_and_builds_the_bot_link(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    with time_machine.travel("2026-03-04 10:00:00+00:00", tick=False):
        token = services.create_link_token(user)
    assert token.expires_at == utc(2026, 3, 4, 10, 15)
    assert services.link_url(token) == f"https://t.me/jobpilot_test_bot?start={token.token}"


def test_a_new_token_withdraws_the_old_unused_one(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    first = services.create_link_token(user)
    second = services.create_link_token(user)
    assert not TelegramLinkToken.objects.filter(pk=first.pk).exists()
    assert TelegramLinkToken.objects.get(pk=second.pk).is_usable()


def test_linking_is_unavailable_without_a_bot(tg, settings) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    settings.TELEGRAM_BOT_USERNAME = ""
    with pytest.raises(TelegramUnavailable):
        services.create_link_token(user)


def test_consuming_a_token_links_the_chat_and_turns_telegram_on(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    token = services.create_link_token(user)
    link = services.consume_link_token(token.token, "777", "janedoe")
    assert (link.user, link.chat_id, link.username, link.active) == (user, "777", "janedoe", True)
    assert UserSettings.objects.get(user=user).notify_telegram is True
    token.refresh_from_db()
    assert token.used_at is not None


def test_a_token_works_once(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    token = services.create_link_token(user)
    services.consume_link_token(token.token, "777")
    with pytest.raises(LinkTokenInvalid):
        services.consume_link_token(token.token, "888")


def test_an_expired_or_unknown_token_is_refused(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    token = services.create_link_token(user)
    later = timezone.now() + datetime.timedelta(minutes=16)
    with time_machine.travel(later, tick=False), pytest.raises(LinkTokenInvalid):
        services.consume_link_token(token.token, "777")
    with pytest.raises(LinkTokenInvalid):
        services.consume_link_token("nonsense", "777")
    assert not TelegramLink.objects.exists()


def test_a_chat_cannot_be_linked_to_two_accounts(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user(chat_id="777")
    other = make_notif_user(chat_id=None, full_name="Other Person")
    token = services.create_link_token(other)
    with pytest.raises(ChatAlreadyLinked):
        services.consume_link_token(token.token, "777")


def test_relinking_replaces_the_users_chat(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id="777")
    token = services.create_link_token(user)
    services.consume_link_token(token.token, "888")
    assert TelegramLink.objects.get(user=user).chat_id == "888"


def test_unlinking_removes_the_link_and_turns_telegram_off(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    services.create_link_token(user)
    services.unlink(user)
    assert not TelegramLink.objects.exists() and not TelegramLinkToken.objects.exists()
    assert UserSettings.objects.get(user=user).notify_telegram is False
    assert services.user_for_chat(CHAT) is None


def test_user_for_chat_ignores_inactive_links_and_accounts(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    assert services.user_for_chat(CHAT) == user
    TelegramLink.objects.filter(user=user).update(active=False)
    assert services.user_for_chat(CHAT) is None
    TelegramLink.objects.filter(user=user).update(active=True)
    user.is_active = False
    user.save()
    assert services.user_for_chat(CHAT) is None


# --- channels -------------------------------------------------------------------------------------


def test_channels_follow_the_users_choices_and_what_works(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    assert services.channels_for(user) == [TG, EMAIL]
    UserSettings.objects.filter(user=user).update(notify_email=False)
    assert services.channels_for(user) == [TG]
    UserSettings.objects.filter(user=user).update(notify_telegram=False, notify_email=True)
    assert services.channels_for(user) == [EMAIL]
    TelegramLink.objects.filter(user=user).delete()
    UserSettings.objects.filter(user=user).update(notify_telegram=True)
    assert services.channels_for(user) == [EMAIL]  # on, but nothing linked


# --- creating notifications -----------------------------------------------------------------------


def test_the_same_key_never_creates_a_second_row(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    first, created = services.create_notification(
        user, kind="system", channel=TG, dedupe_key="k1", payload={"a": 1}
    )
    again, created_again = services.create_notification(
        user, kind="system", channel=TG, dedupe_key="k1", payload={"a": 2}
    )
    assert created and not created_again and again.pk == first.pk and again.payload == {"a": 1}
    assert Notification.objects.count() == 1


def test_different_users_can_share_a_key(tg) -> None:  # type: ignore[no-untyped-def]
    a, b = make_notif_user(), make_notif_user(chat_id="9", full_name="Other Person")
    for user in (a, b):
        services.create_notification(user, kind="system", channel=TG, dedupe_key="same")
    assert Notification.objects.count() == 2


def test_delivery_is_queued_after_commit(tg, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    match = scored_match(user, score=90)
    with django_capture_on_commit_callbacks(execute=True):
        services.request_instant(match)
    assert [m["chat_id"] for m in tg.messages] == [CHAT]  # the immediate Huey task ran
    assert len(mail.outbox) == 1


# --- instant alerts ---------------------------------------------------------------------------------


def test_a_high_score_creates_one_alert_per_channel(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    match = scored_match(user, score=90)
    created = services.request_instant(match)
    assert sorted(n.channel for n in created) == [EMAIL, TG]
    assert {n.dedupe_key for n in created} == {
        f"instant:{match.pk}:telegram",
        f"instant:{match.pk}:email",
    }
    assert all(n.payload == {"match_id": match.pk} for n in created)  # ids only, no text


@pytest.mark.parametrize(
    ("fields", "match_fields"),
    [
        ({"notify_mode": "daily"}, {}),
        ({"notify_mode": "hourly"}, {}),
        ({"notify_mode": "off"}, {}),
        ({"instant_score_threshold": 95}, {}),
        ({}, {"status": "dismissed"}),
        ({}, {"status": "saved"}),
        ({}, {"status": "notified"}),
        ({}, {"rule_pass": False}),
        ({}, {"score": None}),
    ],
)
def test_no_instant_alert_when_the_rules_say_no(tg, fields, match_fields) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(**fields)
    match = scored_match(user, score=match_fields.pop("score", 90), **match_fields)
    assert services.request_instant(match) == []
    assert not Notification.objects.exists()


def test_the_threshold_is_inclusive(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(instant_score_threshold=75)
    assert services.request_instant(scored_match(user, score=74)) == []
    assert (
        services.request_instant(scored_match(user, score=75, job=make_kit_job(external_id="x")))
        != []
    )


def test_rescoring_never_sends_a_second_alert(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    match = scored_match(user, score=90)
    assert services.request_instant(match) != []
    assert services.request_instant(match) == []
    assert Notification.objects.count() == 2


def test_the_alert_goes_only_to_the_channels_that_work(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    created = services.request_instant(scored_match(user, score=90))
    assert [n.channel for n in created] == [EMAIL]


def test_quiet_hours_defer_the_alert_until_they_end(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(
        quiet_hours_start=datetime.time(22, 0), quiet_hours_end=datetime.time(7, 0)
    )
    match = scored_match(user, score=90)
    with time_machine.travel("2026-03-04 20:30:00+00:00", tick=False):  # 23:30 in Nairobi
        created = services.request_instant(match)
        assert all(n.deliver_after == utc(2026, 3, 5, 4, 0) for n in created)
        assert services.deliver(created[0].pk) == services.NOT_DUE
        assert tg.messages == []
    with time_machine.travel("2026-03-05 04:00:01+00:00", tick=False):
        assert services.deliver(created[0].pk) == services.SENT
    assert len(tg.messages) == 1


def test_outside_quiet_hours_the_alert_is_not_delayed(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(
        quiet_hours_start=datetime.time(22, 0), quiet_hours_end=datetime.time(7, 0)
    )
    with time_machine.travel("2026-03-04 09:00:00+00:00", tick=False):
        created = services.request_instant(scored_match(user, score=90))
        assert services.deliver(created[0].pk) == services.SENT


# --- what an instant alert says ------------------------------------------------------------------------


def test_the_telegram_alert_has_the_spec_format_and_buttons(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    job = make_kit_job(
        external_id="fmt", pay_text="$40-60/hr", title="Senior Engineer", company="micro1"
    )
    match = scored_match(user, score=92, job=job)
    services.request_instant(match)
    telegram = next(n for n in rows() if n.channel == TG)
    assert services.deliver(telegram.pk) == services.SENT
    sent = tg.messages[0]
    lines = sent["text"].splitlines()
    assert lines[0] == "🟢 92 · Senior Engineer — micro1"
    assert lines[1].startswith("$40-60/hr · Remote · ")
    assert lines[2] == "Strong: Helpdesk experience · Gap: No Kubernetes"
    assert (
        lines[3].startswith("Posted ")
        and lines[3].endswith(("via Source", "added by you"))
        or "via " in lines[3]
    )
    assert f"/app/jobs/{job.pk}/" in sent["text"]
    labels = [b.text for b in sent["buttons"][0]]
    assert labels[-3:] == ["Kit", "Applied ✓", "Not interested"]
    assert all(len(b.data.encode()) <= 64 for b in sent["buttons"][0] if b.data)


def test_sending_marks_the_match_notified(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    match = scored_match(user, score=90)
    services.request_instant(match)
    for notification in rows():
        services.deliver(notification.pk)
    match.refresh_from_db()
    assert match.status == Match.Status.NOTIFIED and match.notified_at is not None


def test_the_email_alert_is_branded_html_with_a_text_part(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    job = make_kit_job(external_id="mail", title="Support Agent", company="Acme")
    services.request_instant(scored_match(user, score=88, job=job))
    email_row = next(n for n in rows() if n.channel == EMAIL)
    assert services.deliver(email_row.pk) == services.SENT
    message = mail.outbox[0]
    assert message.to == [user.email] and message.subject == "New match: 88 · Support Agent — Acme"
    assert "Support Agent" in message.body and f"/app/jobs/{job.pk}/" in message.body
    html = message.alternatives[0][0]
    assert "<table" in html and "Support Agent" in html and f"/app/jobs/{job.pk}/" in html


def test_a_dismissed_match_is_not_announced(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    match = scored_match(user, score=90)
    services.request_instant(match)
    Match.objects.filter(pk=match.pk).update(status="dismissed")
    assert [services.deliver(n.pk) for n in rows()] == [services.SKIPPED, services.SKIPPED]
    assert tg.messages == [] and mail.outbox == []


# --- delivery: idempotency, retries, failures -----------------------------------------------------------


def test_a_sent_notification_is_never_sent_again(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    assert services.deliver(notification.pk) == services.SENT
    assert services.deliver(notification.pk) == "sent"
    assert len(tg.messages) == 1
    notification.refresh_from_db()
    assert notification.status == Notification.Status.SENT and notification.sent_at is not None


def test_a_row_being_sent_by_another_worker_is_left_alone(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    Notification.objects.filter(pk=notification.pk).update(claimed_at=timezone.now())
    assert services.deliver(notification.pk) == services.BUSY and tg.messages == []
    Notification.objects.filter(pk=notification.pk).update(
        claimed_at=timezone.now() - datetime.timedelta(minutes=11)
    )
    assert services.deliver(notification.pk) == services.SENT  # a dead worker's claim expires


def test_a_missing_row_is_gone() -> None:
    assert services.deliver(987654) == services.GONE


def test_a_telegram_error_is_retried_then_given_up(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    now = timezone.now()
    for attempt in (1, 2):
        tg.fail_with = GatewayError("NetworkError")
        with time_machine.travel(now + datetime.timedelta(minutes=10 * attempt), tick=False):
            assert services.deliver(notification.pk) == services.RETRY
        notification.refresh_from_db()
        assert (
            notification.status == Notification.Status.PENDING and notification.attempts == attempt
        )
    tg.fail_with = GatewayError("NetworkError")
    with time_machine.travel(now + datetime.timedelta(minutes=40), tick=False):
        assert services.deliver(notification.pk) == services.FAILED
    notification.refresh_from_db()
    assert (
        notification.status == Notification.Status.FAILED and notification.error == "NetworkError"
    )
    assert tg.messages == []


def test_a_blocked_bot_switches_telegram_off(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    tg.fail_with = GatewayError("forbidden", blocked=True)
    assert services.deliver(notification.pk) == services.FAILED
    assert TelegramLink.objects.get(user=user).active is False
    assert UserSettings.objects.get(user=user).notify_telegram is False
    assert services.channels_for(user) == []


def test_an_email_error_is_retried(tg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    notification = services.request_instant(scored_match(user, score=90))[0]

    def refuse(self: Any, fail_silently: bool = False) -> int:
        raise smtplib.SMTPServerDisconnected("down")

    monkeypatch.setattr("django.core.mail.EmailMultiAlternatives.send", refuse)
    assert services.deliver(notification.pk) == services.RETRY
    notification.refresh_from_db()
    assert (
        notification.status == Notification.Status.PENDING
        and notification.error == "SMTPServerDisconnected"
    )


def test_an_unexpected_error_fails_the_row_without_raising(tg, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    monkeypatch.setattr("apps.notifications.render.instant_text", lambda *a, **k: 1 / 0)
    assert services.deliver(notification.pk) == services.FAILED
    notification.refresh_from_db()
    assert notification.error == "internal_error"


def test_an_unlinked_chat_skips_the_telegram_alert(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    notification = services.request_instant(scored_match(user, score=90))[0]
    TelegramLink.objects.filter(user=user).delete()
    assert services.deliver(notification.pk) == services.SKIPPED and tg.messages == []


# --- waiting for the kit (spec §10.4) -----------------------------------------------------------------------


def test_the_alert_waits_up_to_90_seconds_for_a_kit_in_progress(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    job = make_kit_job(external_id="waitkit")
    match = scored_match(user, score=95, job=job)
    notification = services.request_instant(match)[0]
    kit_services.request_kit(user, job, match=match, auto=True)  # queued, not yet run
    created = notification.created_at

    with time_machine.travel(created + datetime.timedelta(seconds=30), tick=False):
        assert services.deliver(notification.pk) == services.RETRY
    Notification.objects.filter(pk=notification.pk).update(deliver_after=created)
    with time_machine.travel(created + datetime.timedelta(seconds=91), tick=False):
        assert services.deliver(notification.pk) == services.SENT
    assert len(tg.messages) == 1 and "ready" not in tg.messages[0]["text"].lower().split("\n")[-2]
    assert tg.documents == []  # no kit yet: nothing to attach


def test_a_ready_kit_is_mentioned_and_its_pdfs_are_attached(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    job = make_kit_job(external_id="withkit")
    match = scored_match(user, score=95, job=job)
    from tests.kit_helpers import Ids  # noqa: F401

    ids = _ids_of(user)
    script_kit(fake_llm, good_cv(ids))
    kit = kit_services.request_kit(user, job, match=match, auto=True)
    kit_services.generate_kit(kit.pk)
    notification = services.request_instant(match)[0]
    assert services.deliver(notification.pk) == services.SENT
    message = tg.messages[0]
    assert "Your CV and cover letter are ready." in message["text"]
    assert message["buttons"][0][-3].text == "Kit ✓"
    assert [d["filename"] for d in tg.documents] == [
        "Jane_Doe_CV_Globex-Corp.pdf",
        "Jane_Doe_CoverLetter_Globex-Corp.pdf",
    ]
    assert tg.documents[0]["data"].startswith(b"%PDF")


def test_documents_are_not_sent_when_the_user_turned_that_off(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False, send_documents_telegram=False)
    job = make_kit_job(external_id="nodocs")
    match = scored_match(user, score=95, job=job)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit_services.generate_kit(kit_services.request_kit(user, job, match=match).pk)
    assert services.deliver(services.request_instant(match)[0].pk) == services.SENT
    assert tg.documents == []


def _ids_of(user: Any) -> Any:
    """Item IDs of a `make_kit_user` profile, read back from the database."""
    from apps.profiles.models import (
        Certification,
        Education,
        Experience,
        ExperienceBullet,
        Project,
        Skill,
    )
    from apps.profiles.serializers import ref
    from tests.kit_helpers import Ids

    experiences = list(Experience.objects.for_user(user).order_by("order"))
    bullets = {b.text: b for b in ExperienceBullet.objects.for_user(user)}
    skills = {s.name: s for s in Skill.objects.for_user(user)}
    return Ids(
        exp=ref("EXP", experiences[0].pk), exp2=ref("EXP", experiences[1].pk),
        bul1=ref("BUL", bullets["Resolved support tickets for 120 staff"].pk),
        bul2=ref("BUL", bullets["Set up Windows laptops for new staff"].pk),
        bul3=ref("BUL", bullets["Wrote guides for the IT team"].pk),
        prj=ref("PRJ", Project.objects.for_user(user).get().pk),
        skl_helpdesk=ref("SKL", skills["Helpdesk"].pk), skl_excel=ref("SKL", skills["Excel"].pk),
        skl_python=ref("SKL", skills["Python"].pk),
        edu=ref("EDU", Education.objects.for_user(user).get().pk),
        crt=ref("CRT", Certification.objects.for_user(user).get().pk),
    )  # fmt: skip


# --- digests ---------------------------------------------------------------------------------------------------


def test_the_digest_lists_new_rule_passed_matches_best_first_with_a_cap(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily")
    for i in range(17):
        scored_match(user, score=40 + i, job=make_kit_job(external_id=f"d{i}"))
    scored_match(user, score=39, job=make_kit_job(external_id="low"))  # below 40: left out
    scored_match(user, score=99, job=make_kit_job(external_id="seen"), status="seen")
    scored_match(user, score=99, job=make_kit_job(external_id="rp"), rule_pass=False)
    unscored = scored_match(user, score=50, job=make_kit_job(external_id="uns"))
    Match.objects.filter(pk=unscored.pk).update(score=None)
    matches, more = services.digest_matches(user)
    assert len(matches) == 15 and more == 3
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True) and scores[0] == 56


def test_unscored_matches_appear_last_in_the_digest(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily")
    unscored = scored_match(user, score=50, job=make_kit_job(external_id="u1"))
    Match.objects.filter(pk=unscored.pk).update(score=None)
    scored_match(user, score=45, job=make_kit_job(external_id="u2"))
    matches, _ = services.digest_matches(user)
    assert [m.score for m in matches] == [45, None]


def test_a_digest_goes_to_each_channel_and_marks_matches_notified(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily")
    first = scored_match(user, score=80, job=make_kit_job(external_id="g1", title="Alpha"))
    second = scored_match(user, score=60, job=make_kit_job(external_id="g2", title="Beta"))
    created = services.send_digest(user, "d:2026-03-04")
    assert sorted(n.channel for n in created) == [EMAIL, TG]
    for notification in created:
        assert services.deliver(notification.pk) == services.SENT
    text = tg.messages[0]["text"]
    assert text.startswith("📬 2 new matches") and text.index("Alpha") < text.index("Beta")
    assert mail.outbox[0].subject.startswith("2 new matches")
    assert "Alpha" in mail.outbox[0].body and "Alpha" in mail.outbox[0].alternatives[0][0]
    for match in (first, second):
        match.refresh_from_db()
        assert match.status == Match.Status.NOTIFIED


def test_a_digest_with_more_than_fifteen_says_so(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    for i in range(18):
        scored_match(user, score=50 + i, job=make_kit_job(external_id=f"m{i}"))
    notification = services.send_digest(user, "d:x")[0]
    services.deliver(notification.pk)
    assert "…and 3 more on your dashboard" in tg.messages[0]["text"]
    assert tg.messages[0]["text"].startswith("📬 18 new matches")


def test_an_empty_period_is_used_up_without_a_message(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily")
    now = utc(2026, 3, 4, 5, 0)
    assert services.send_digest(user, "d:2026-03-04", now=now) == []
    assert UserSettings.objects.get(user=user).last_digest_sent_at == now
    assert not Notification.objects.exists()


def test_the_same_period_never_makes_two_digests(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily")
    scored_match(user, score=70)
    assert len(services.send_digest(user, "d:2026-03-04")) == 2
    assert services.send_digest(user, "d:2026-03-04") == []


def test_matches_dismissed_before_sending_are_dropped_from_the_digest(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    keep = scored_match(user, score=70, job=make_kit_job(external_id="k1", title="Keep"))
    drop = scored_match(user, score=80, job=make_kit_job(external_id="k2", title="Drop"))
    notification = services.send_digest(user, "d:y")[0]
    Match.objects.filter(pk=drop.pk).update(status="dismissed")
    services.deliver(notification.pk)
    assert "Keep" in tg.messages[0]["text"] and "Drop" not in tg.messages[0]["text"]
    keep.refresh_from_db()
    assert keep.status == Match.Status.NOTIFIED


def test_a_manual_digest_does_not_use_up_the_days_scheduled_one(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    scored_match(user, score=70)
    created = services.send_digest_now(user)
    assert len(created) == 1 and created[0].dedupe_key.startswith("digest:manual:")
    assert UserSettings.objects.get(user=user).last_digest_sent_at is None


def test_pressing_digest_twice_in_a_minute_sends_once(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 10:00:10+00:00", tick=False):
        first = services.send_digest_now(user)
        second = services.send_digest_now(user)
    assert len(first) == 1 and second == []


# --- the dispatcher ---------------------------------------------------------------------------------------------


def test_the_dispatcher_sends_the_daily_digest_once_at_the_chosen_local_time(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", digest_time=datetime.time(8, 0))
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 04:55:00+00:00", tick=False):  # 07:55 in Nairobi
        assert services.dispatch()["digests"] == 0
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):  # 08:00 in Nairobi
        counts = services.dispatch()
    assert counts["digests"] == 1 and counts["sent"] == 2
    assert len(tg.messages) == 1 and len(mail.outbox) == 1
    for moment in ("05:05:00", "05:10:00", "12:00:00", "20:00:00"):  # retries and later ticks
        with time_machine.travel(f"2026-03-04 {moment}+00:00", tick=False):
            services.dispatch()
    assert len(tg.messages) == 1 and len(mail.outbox) == 1
    assert Notification.objects.filter(kind="digest").count() == 2


def test_a_digest_is_sent_again_the_next_day(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):
        services.dispatch()
    scored_match(user, score=65, job=make_kit_job(external_id="tomorrow"))
    with time_machine.travel("2026-03-05 05:00:00+00:00", tick=False):
        services.dispatch()
    assert len(tg.messages) == 2


def test_a_second_dispatcher_run_in_the_same_moment_adds_nothing(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="daily", notify_email=False)
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):
        services.dispatch()
        UserSettings.objects.filter(user=user).update(last_digest_sent_at=None)  # a lost update
        services.dispatch()
    assert len(tg.messages) == 1  # the dedupe key still stopped the repeat


def test_an_hourly_user_gets_one_message_an_hour_when_there_is_news(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="hourly", notify_email=False)
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 09:00:00+00:00", tick=False):
        services.dispatch()
    scored_match(user, score=66, job=make_kit_job(external_id="h2"))
    with time_machine.travel("2026-03-04 09:30:00+00:00", tick=False):
        services.dispatch()
    assert len(tg.messages) == 1
    with time_machine.travel("2026-03-04 10:00:00+00:00", tick=False):
        services.dispatch()
    assert len(tg.messages) == 2
    with time_machine.travel("2026-03-04 11:00:00+00:00", tick=False):  # nothing new
        services.dispatch()
    assert len(tg.messages) == 2


def test_hourly_messages_wait_out_quiet_hours(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(
        notify_mode="hourly", notify_email=False,
        quiet_hours_start=datetime.time(22, 0), quiet_hours_end=datetime.time(7, 0),
    )  # fmt: skip
    scored_match(user, score=70)
    with time_machine.travel("2026-03-04 20:30:00+00:00", tick=False):  # 23:30 in Nairobi
        services.dispatch()
    assert tg.messages == []
    with time_machine.travel("2026-03-05 04:05:00+00:00", tick=False):  # 07:05
        services.dispatch()
    assert len(tg.messages) == 1


def test_off_users_get_nothing_but_the_dashboard_keeps_matches(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="off")
    match = scored_match(user, score=99)
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):
        assert services.dispatch()["digests"] == 0
    assert tg.messages == [] and mail.outbox == []
    match.refresh_from_db()
    assert match.status == Match.Status.NEW


def test_instant_users_get_everything_below_the_threshold_in_the_daily_digest(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="instant", notify_email=False)
    sent_instantly = scored_match(
        user, score=90, job=make_kit_job(external_id="hi", title="Instant One")
    )
    scored_match(user, score=55, job=make_kit_job(external_id="lo", title="Digest One"))
    for notification in services.request_instant(sent_instantly):
        services.deliver(notification.pk)
    tg.reset()
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):
        services.dispatch()
    assert len(tg.messages) == 1
    assert "Digest One" in tg.messages[0]["text"] and "Instant One" not in tg.messages[0]["text"]


def test_inactive_users_and_users_without_channels_are_skipped(tg) -> None:  # type: ignore[no-untyped-def]
    gone = make_notif_user(notify_mode="daily")
    scored_match(gone, score=70)
    gone.is_active = False
    gone.save()
    quiet = make_notif_user(
        chat_id=None, notify_mode="daily", notify_email=False, full_name="No Channels"
    )
    scored_match(quiet, score=70, job=make_kit_job(external_id="q"))
    with time_machine.travel("2026-03-04 05:00:00+00:00", tick=False):
        assert services.dispatch() == {"due": 0, "digests": 1, "sent": 0}
    assert UserSettings.objects.get(user=quiet).last_digest_sent_at is None  # nothing was used up


def test_the_dispatcher_releases_deferred_and_missed_notifications(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(
        notify_email=False,
        quiet_hours_start=datetime.time(22, 0),
        quiet_hours_end=datetime.time(7, 0),
    )
    with time_machine.travel("2026-03-04 20:30:00+00:00", tick=False):
        services.request_instant(scored_match(user, score=90))
        assert services.dispatch()["due"] == 0
    with time_machine.travel("2026-03-05 04:00:30+00:00", tick=False):
        counts = services.dispatch()
    assert counts["due"] == 1 and counts["sent"] == 1 and len(tg.messages) == 1


# --- kit-ready messages -------------------------------------------------------------------------------------------


def test_the_kit_button_sends_ready_documents_at_once(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    job = make_kit_job(external_id="btn1")
    match = scored_match(user, score=90, job=job)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit_services.generate_kit(kit_services.request_kit(user, job, match=match).pk)
    assert services.kit_for_button(user, match) == "ready"
    assert len(tg.documents) == 2


def test_the_kit_button_queues_a_kit_and_sends_it_when_ready(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    job = make_kit_job(external_id="btn2")
    match = scored_match(user, score=70, job=job)
    assert services.kit_for_button(user, match) == "queued"
    kit = Kit.objects.get(user=user, job=job)
    pending = rows(kind="kit_ready")
    assert [n.channel for n in pending] == [TG]  # asked on Telegram: answered on Telegram
    assert services.kit_for_button(user, match) == "queued"  # pressing again adds nothing
    assert Kit.objects.count() == 1 and len(rows(kind="kit_ready")) == 1

    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit_services.generate_kit(kit.pk)  # the after-generate hook delivers the waiting message
    assert len(tg.messages) == 1 and "is ready" in tg.messages[0]["text"]
    assert len(tg.documents) == 2
    assert rows(kind="kit_ready")[0].status == Notification.Status.SENT


def test_a_failed_kit_tells_the_telegram_user(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    job = make_kit_job(external_id="btn3")
    match = scored_match(user, score=70, job=job)
    services.kit_for_button(user, match)
    kit = Kit.objects.get(user=user)
    fake_llm.script.extend(["nope", "nope"])
    kit_services.generate_kit(kit.pk)
    services.deliver(rows(kind="kit_ready")[0].pk)
    assert "could not be prepared" in tg.messages[-1]["text"] and tg.documents == []


def test_the_kit_button_reports_the_monthly_limit_and_an_empty_profile(tg) -> None:  # type: ignore[no-untyped-def]
    from apps.llm.models import UsageQuota

    user = make_notif_user()
    match = scored_match(user, score=70, job=make_kit_job(external_id="lim"))
    UsageQuota.objects.create(user=user, monthly_kits=0)
    assert services.kit_for_button(user, match) == "limit"
    empty = UserFactory()
    UserSettings.objects.create(user=empty)
    empty_match = scored_match(empty, score=70, job=make_kit_job(external_id="emp"))
    assert services.kit_for_button(empty, empty_match) == "empty"


def test_a_late_auto_kit_gets_a_follow_up_message(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    job = make_kit_job(external_id="late")
    match = scored_match(user, score=95, job=job)
    kit = kit_services.request_kit(user, job, match=match, auto=True)
    alert = services.request_instant(match)[0]
    with time_machine.travel(alert.created_at + datetime.timedelta(seconds=120), tick=False):
        assert services.deliver(alert.pk) == services.SENT  # sent without the kit after 90 s
    assert len(tg.messages) == 1
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit_services.generate_kit(kit.pk)
    assert len(tg.messages) == 2 and "is ready" in tg.messages[1]["text"]
    assert len(tg.documents) == 2


def test_kit_ready_by_email_is_a_link_only(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(chat_id=None)
    job = make_kit_job(external_id="em")
    kit = kit_services.request_kit(user, job)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit_services.generate_kit(kit.pk)
    (row,) = services._kit_ready_rows(user, kit)
    assert services.deliver(row.pk) == services.SENT
    message = mail.outbox[0]
    assert f"/app/jobs/{job.pk}/#kit" in message.body and message.attachments == []


# --- staff notices -------------------------------------------------------------------------------------------------


def test_staff_with_telegram_hear_about_failing_sources_once_a_day(tg) -> None:  # type: ignore[no-untyped-def]
    staff = make_notif_user(chat_id="42", full_name="Owner")
    staff.is_staff = True
    staff.save()
    plain = make_notif_user(chat_id="43", full_name="Plain User")
    day = datetime.date(2026, 3, 4)
    from tests.factories import SourceFactory

    source = SourceFactory(display_name="ExpertWoka", health="failing", consecutive_failures=4)
    assert services.notify_source_health(source.pk, "failing", day) == 1
    assert services.notify_source_health(source.pk, "failing", day) == 0  # same day: no repeat
    assert services.notify_source_health(source.pk, "failing", datetime.date(2026, 3, 5)) == 1
    for row in rows(user=staff):
        services.deliver(row.pk)
    assert tg.messages[0]["chat_id"] == "42"
    assert "ExpertWoka is failing (4 failures in a row)" in tg.messages[0]["text"]
    assert not Notification.objects.filter(user=plain).exists()


def test_staff_hear_about_waitlist_requests(tg) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts.services import submit_waitlist

    staff = make_notif_user(chat_id="42", full_name="Owner")
    staff.is_staff = True
    staff.save()
    entry, _ = submit_waitlist(name="Wanjiku", email="w@example.com", reason="Looking for work")
    assert services.notify_new_waitlist_entry(entry.pk) == 1
    services.deliver(rows(user=staff)[0].pk)
    text = tg.messages[0]["text"]
    assert "Wanjiku" in text and f"/admin/accounts/waitlistentry/{entry.pk}/change/" in text
    assert "w@example.com" not in text and "Looking for work" not in text


def test_no_staff_notice_without_linked_staff(tg) -> None:  # type: ignore[no-untyped-def]
    make_notif_user()
    assert services.notify_new_waitlist_entry(1) == 0
    assert json.dumps(list(Notification.objects.values())) == "[]"


def test_a_match_with_a_pending_instant_alert_is_not_also_in_the_digest(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    waiting = scored_match(user, score=90, job=make_kit_job(external_id="w1"))
    other = scored_match(user, score=50, job=make_kit_job(external_id="w2"))
    services.request_instant(waiting)
    matches, _ = services.digest_matches(user)
    assert [m.pk for m in matches] == [other.pk]
    Notification.objects.filter(kind="instant_match").update(status="failed")
    matches, _ = services.digest_matches(user)  # a failed alert is caught up by the digest
    assert {m.pk for m in matches} == {waiting.pk, other.pk}


def test_a_test_alert_goes_out_for_the_latest_match_whatever_its_state(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="off")
    match = scored_match(user, score=30, status="saved", rule_pass=False)
    assert services.send_test_alert(user) == services.SENT
    assert len(tg.messages) == 1 and tg.messages[0]["chat_id"] == CHAT
    match.refresh_from_db()
    assert match.status == Match.Status.SAVED  # untouched
    assert services.send_test_alert(user, now=utc(2030, 1, 1)) == services.SENT  # not deduped


def test_a_test_alert_explains_what_is_missing(tg) -> None:  # type: ignore[no-untyped-def]
    assert services.send_test_alert(make_notif_user(chat_id=None)) == "no_telegram"
    assert (
        services.send_test_alert(make_notif_user(chat_id="2", full_name="No Matches")) == "no_match"
    )
