"""Applications, stage changes, interviews, follow-ups and stats (spec §14, §13.5)."""

import datetime
from typing import Any

import pytest
import time_machine
from django.core import mail

from apps.kits import services as kit_services
from apps.kits.models import Kit
from apps.matching.models import Match
from apps.notifications import services as notification_services
from apps.notifications.models import Notification
from apps.tracker import services, stats
from apps.tracker.exceptions import InvalidKit, InvalidStage, NotOwner
from apps.tracker.models import Application, ApplicationEvent, Interview, Stage
from tests.factories import MatchFactory, SourceFactory, TrackFactory, UserFactory
from tests.kit_helpers import good_cv, make_kit_job, script_kit
from tests.notif_helpers import make_notif_user, utc
from tests.test_notifications_services import _ids_of

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
NOW = utc(2026, 3, 4, 10, 0)


@pytest.fixture
def me(tg) -> tuple[Any, Any]:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    return user, make_kit_job(external_id="trk")


def make_ready_kit(user: Any, job: Any, fake_llm: Any) -> Kit:
    script_kit(fake_llm, good_cv(_ids_of(user)))
    kit = kit_services.request_kit(user, job)
    kit_services.generate_kit(kit.pk)
    kit.refresh_from_db()
    return kit


def seed(
    user: Any, job: Any, stage: str = "applied", *, applied: datetime.datetime = NOW, **fields: Any
) -> Application:
    """An application with its history, without going through the services."""
    application = Application.objects.create(
        user=user, job=job, stage=stage, applied_at=applied, last_stage_change_at=applied,
        next_followup_at=applied + services.FOLLOWUP_AFTER if stage == "applied" else None, **fields,
    )  # fmt: skip
    ApplicationEvent.objects.create(
        user=user, application=application, from_stage="", to_stage="applied", at=applied
    )
    return application


# --- applying ------------------------------------------------------------------------------------------


def test_applying_creates_one_application_with_its_first_event(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, created = services.apply_to_job(user, job, applied_at=NOW)
    assert created and application.stage == Stage.APPLIED and application.applied_at == NOW
    assert application.next_followup_at == NOW + datetime.timedelta(days=7)
    event = application.events.get()
    assert (event.from_stage, event.to_stage, event.at) == ("", "applied", NOW)


def test_applying_twice_returns_the_same_application(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    first, _ = services.apply_to_job(user, job)
    second, created = services.apply_to_job(user, job)
    assert second.pk == first.pk and not created
    assert Application.objects.count() == 1 and ApplicationEvent.objects.count() == 1


def test_the_latest_ready_kit_is_linked(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    make_ready_kit(user, job, fake_llm)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    second = kit_services.request_kit(user, job)
    kit_services.generate_kit(second.pk)
    application, _ = services.apply_to_job(user, job)
    assert application.kit == Kit.objects.get(user=user, version=2)


def test_no_kit_means_no_link(me) -> None:  # type: ignore[no-untyped-def]
    application, _ = services.apply_to_job(*me)
    assert application.kit is None


def test_a_kit_that_is_not_ready_is_not_linked(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    kit_services.request_kit(user, job)  # queued only
    assert services.apply_to_job(user, job)[0].kit is None


def test_applying_marks_the_match_applied_and_copies_its_track(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    track = TrackFactory(user=user, name="Second track")
    match = MatchFactory(user=user, job=job, track=track, score=80, status="new")
    application, _ = services.apply_to_job(user, job, match=match)
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED and application.track == track


def test_the_match_is_found_when_not_passed(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    match = MatchFactory(user=user, job=job, status="new")
    services.apply_to_job(user, job)
    match.refresh_from_db()
    assert match.status == Match.Status.APPLIED


def test_a_saved_application_becomes_applied(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    saved = Application.objects.create(
        user=user, job=job, stage=Stage.SAVED, last_stage_change_at=NOW - datetime.timedelta(days=3)
    )
    application, created = services.apply_to_job(user, job, applied_at=NOW, channel="Site")
    assert application.pk == saved.pk and not created
    assert (application.stage, application.applied_at, application.channel) == (
        "applied",
        NOW,
        "Site",
    )
    assert application.next_followup_at == NOW + datetime.timedelta(days=7)
    assert application.events.get().from_stage == "saved"


def test_an_application_further_along_is_left_alone(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application = seed(user, job, "interview")
    again, created = services.apply_to_job(user, job)
    assert again.pk == application.pk and not created and again.stage == "interview"


def test_other_peoples_private_jobs_cannot_be_applied_to(me) -> None:  # type: ignore[no-untyped-def]
    user, _ = me
    other = UserFactory()
    private = make_kit_job(visibility="private", owner=other, source=None, external_id="p")
    with pytest.raises(NotOwner):
        services.apply_to_job(user, private)
    assert not Application.objects.exists()


def test_someone_elses_match_cannot_be_used(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    theirs = MatchFactory(user=UserFactory(), job=job)
    with pytest.raises(NotOwner):
        services.apply_to_job(user, job, match=theirs)


# --- manual applications -----------------------------------------------------------------------------------


def test_a_manual_application_makes_a_private_job(me) -> None:  # type: ignore[no-untyped-def]
    user, _ = me
    application = services.add_manual_application(
        user, title="  Support Lead ", company="Acme", url="https://acme.example/jobs/1",
        applied_on=datetime.date(2026, 2, 20), channel="Email", notes="Spoke to Sam",
    )  # fmt: skip
    job = application.job
    assert (job.title, job.company, job.visibility, job.owner) == (
        "Support Lead",
        "Acme",
        "private",
        user,
    )
    assert application.applied_at == utc(2026, 2, 20, 12, 0)
    assert application.next_followup_at == utc(2026, 2, 27, 12, 0)
    assert (application.channel, application.notes) == ("Email", "Spoke to Sam")
    assert application.events.count() == 1


def test_manual_applications_do_not_create_matches(me) -> None:  # type: ignore[no-untyped-def]
    user, _ = me
    services.add_manual_application(
        user, title="T", company="C", applied_on=datetime.date(2026, 2, 1)
    )
    assert not Match.objects.exists()


# --- stage changes -----------------------------------------------------------------------------------------------


def test_changing_stage_records_an_event_and_dates(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job, applied_at=NOW)
    later = NOW + datetime.timedelta(days=5)
    services.change_stage(user, application, "screening", "Recruiter called", now=later)
    application.refresh_from_db()
    assert application.stage == "screening" and application.last_stage_change_at == later
    assert application.next_followup_at is None  # a reply came: no more reminders
    event = application.events.order_by("-id").first()
    assert (event.from_stage, event.to_stage, event.note, event.at) == (
        "applied", "screening", "Recruiter called", later,
    )  # fmt: skip


def test_the_same_stage_is_a_no_op(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job)
    services.change_stage(user, application, "applied")
    assert application.events.count() == 1


@pytest.mark.parametrize("bad", ["", "Applied", "done", "applied; DROP", "x" * 50])
def test_unknown_stages_are_refused(me, bad: str) -> None:  # type: ignore[no-untyped-def]
    application, _ = services.apply_to_job(*me)
    with pytest.raises(InvalidStage):
        services.change_stage(me[0], application, bad)
    assert application.events.count() == 1


@pytest.mark.parametrize("stage", [s.value for s in Stage])
def test_every_stage_can_be_reached(me, stage: str) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application = seed(user, job, "withdrawn" if stage == "applied" else "applied")
    services.change_stage(user, application, stage)
    application.refresh_from_db()
    assert application.stage == stage


def test_moving_back_to_applied_restarts_the_follow_up_and_marks_the_match(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    match = MatchFactory(user=user, job=job, status="seen")
    application = seed(user, job, "saved", applied=NOW)
    Application.objects.filter(pk=application.pk).update(applied_at=None, stage="saved")
    application.refresh_from_db()
    later = NOW + datetime.timedelta(days=2)
    services.change_stage(user, application, "applied", now=later)
    application.refresh_from_db()
    match.refresh_from_db()
    assert (
        application.applied_at == later
        and application.next_followup_at == later + datetime.timedelta(days=7)
    )
    assert match.status == Match.Status.APPLIED


def test_only_the_owner_can_change_a_stage(me) -> None:  # type: ignore[no-untyped-def]
    application, _ = services.apply_to_job(*me)
    with pytest.raises(NotOwner):
        services.change_stage(UserFactory(), application, "offer")


def test_the_event_history_is_ordered_and_complete(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job, applied_at=NOW)
    for days, stage in ((1, "screening"), (3, "interview"), (6, "offer")):
        services.change_stage(user, application, stage, now=NOW + datetime.timedelta(days=days))
    assert [(e.from_stage, e.to_stage) for e in application.events.all()] == [
        ("", "applied"), ("applied", "screening"), ("screening", "interview"), ("interview", "offer"),
    ]  # fmt: skip


# --- details, kit choice, interviews ---------------------------------------------------------------------------------


def test_details_are_trimmed_and_saved(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job)
    services.update_details(
        user,
        application,
        notes="  call Sam ",
        channel="Referral",
        outcome_reason="Budget",
        salary_offered="$900",
    )
    application.refresh_from_db()
    assert (application.notes, application.channel, application.outcome_reason, application.salary_offered) == (
        "call Sam", "Referral", "Budget", "$900",
    )  # fmt: skip


def test_the_kit_can_be_changed_to_another_ready_version_or_cleared(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    first = make_ready_kit(user, job, fake_llm)
    script_kit(fake_llm, good_cv(_ids_of(user)))
    second = kit_services.request_kit(user, job)
    kit_services.generate_kit(second.pk)
    application, _ = services.apply_to_job(user, job)
    assert application.kit_id == second.pk
    services.set_kit(user, application, first.pk)
    assert Application.objects.get(pk=application.pk).kit_id == first.pk
    services.set_kit(user, application, None)
    assert Application.objects.get(pk=application.pk).kit is None
    assert [k.version for k in services.ready_kits(user, application)] == [2, 1]


def test_a_kit_of_another_job_or_user_or_not_ready_is_refused(me, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job)
    other_job_kit = make_ready_kit(user, make_kit_job(external_id="elsewhere"), fake_llm)
    queued = kit_services.request_kit(user, job)
    stranger, stranger_job = (
        make_notif_user(chat_id="9", full_name="Stranger Person"),
        make_kit_job(external_id="s"),
    )
    their_kit = Kit.objects.create(user=stranger, job=stranger_job, status="ready")
    for kit_id in (other_job_kit.pk, queued.pk, their_kit.pk, 99999):
        with pytest.raises(InvalidKit):
            services.set_kit(user, application, kit_id)


def test_interviews_can_be_added_edited_and_deleted(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job)
    interview = services.add_interview(user, application, kind="phone", scheduled_at=NOW, notes="n")
    services.update_interview(user, interview, outcome="Went well", questions_asked="Why us?")
    interview.refresh_from_db()
    assert (interview.kind, interview.outcome, interview.questions_asked) == (
        "phone",
        "Went well",
        "Why us?",
    )
    services.delete_interview(user, interview)
    assert not Interview.objects.exists()


def test_other_users_cannot_touch_interviews_or_application_details(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application, _ = services.apply_to_job(user, job)
    interview = services.add_interview(user, application, kind="phone")
    intruder = UserFactory()
    with pytest.raises(NotOwner):
        services.get_interview(intruder, interview.pk)
    with pytest.raises(NotOwner):
        services.get_application(intruder, application.pk)
    for call in (
        lambda: services.update_details(intruder, application, notes="x"),
        lambda: services.add_interview(intruder, application, kind="phone"),
        lambda: services.update_interview(intruder, interview, outcome="x"),
        lambda: services.delete_interview(intruder, interview),
        lambda: services.set_kit(intruder, application, None),
    ):
        with pytest.raises(NotOwner):
            call()


def test_the_board_groups_applications_by_stage(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    seed(user, job, "applied")
    seed(user, make_kit_job(external_id="b2"), "offer")
    board = services.board(user)
    assert [len(board[s.value]) for s in Stage] == [0, 1, 0, 0, 1, 0, 0, 0, 0]
    assert services.stage_counts(user)["applied"] == 1


# --- the ghosted suggestion ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("days", "expected"), [(20, False), (21, True), (40, True)])
def test_ghosted_is_suggested_after_21_quiet_days(me, days: int, expected: bool) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application = seed(user, job, applied=NOW - datetime.timedelta(days=days))
    assert services.suggests_ghosted(application, NOW) is expected


def test_ghosted_is_only_suggested_for_applied(me) -> None:  # type: ignore[no-untyped-def]
    user, job = me
    application = seed(user, job, "interview", applied=NOW - datetime.timedelta(days=40))
    assert services.suggests_ghosted(application, NOW) is False


# --- follow-up reminders ---------------------------------------------------------------------------------------------------


def due_app(user: Any, days_ago: int = 8) -> Application:
    start = NOW - datetime.timedelta(days=days_ago)
    return seed(user, make_kit_job(external_id=f"due{days_ago}"), applied=start)


def test_a_due_application_gets_one_reminder_per_channel_and_a_new_date(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    application = due_app(user)
    with time_machine.travel(NOW, tick=False):
        counts = services.send_followup_reminders()
    assert counts["reminders"] == 1
    rows = Notification.objects.filter(kind="followup_reminder")
    assert sorted(r.channel for r in rows) == ["email", "telegram"]
    assert all(r.payload == {"application_id": application.pk, "what": "reminder"} for r in rows)
    application.refresh_from_db()
    assert application.next_followup_at == NOW + datetime.timedelta(days=7)


def test_reminders_are_sent_once_and_rescheduled_a_week_later(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    due_app(user)
    with time_machine.travel(NOW, tick=False):
        services.send_followup_reminders()
        services.send_followup_reminders()  # a second run the same day
        for row in Notification.objects.all():
            notification_services.deliver(row.pk)
    assert len(tg.messages) == 1 and len(mail.outbox) == 1
    assert Notification.objects.count() == 2
    with time_machine.travel(NOW + datetime.timedelta(days=3), tick=False):
        services.send_followup_reminders()  # not due again yet
    assert Notification.objects.count() == 2
    with time_machine.travel(NOW + datetime.timedelta(days=7, minutes=1), tick=False):
        services.send_followup_reminders()  # a week later: one more
    assert Notification.objects.filter(kind="followup_reminder").count() == 4


def test_the_reminder_says_what_to_follow_up(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    application = due_app(user)
    with time_machine.travel(NOW, tick=False):
        services.send_followup_reminders()
        for row in Notification.objects.all():
            notification_services.deliver(row.pk)
    text = tg.messages[0]["text"]
    assert "Time to follow up on Remote IT Support Engineer at Globex Corp" in text
    assert "8 days ago" in text and f"/app/tracker/{application.pk}/" in text
    assert "Follow up on Remote IT Support Engineer" in mail.outbox[0].subject
    assert f"/app/tracker/{application.pk}/" in mail.outbox[0].alternatives[0][0]


def test_applications_that_moved_on_get_no_reminder(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    application = due_app(user)
    services.change_stage(user, application, "screening", now=NOW)
    with time_machine.travel(NOW, tick=False):
        assert services.send_followup_reminders()["reminders"] == 0
    assert not Notification.objects.exists()


def test_a_reminder_queued_before_the_user_moved_on_is_skipped(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    application = due_app(user)
    with time_machine.travel(NOW, tick=False):
        services.send_followup_reminders()
    services.change_stage(user, application, "rejected", now=NOW)
    outcomes = [notification_services.deliver(n.pk, now=NOW) for n in Notification.objects.all()]
    assert outcomes == ["skipped", "skipped"] and tg.messages == []


def test_users_with_alerts_off_are_not_reminded_but_the_date_still_moves(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_mode="off")
    application = due_app(user)
    with time_machine.travel(NOW, tick=False):
        services.send_followup_reminders()
    assert not Notification.objects.exists()
    application.refresh_from_db()
    assert application.next_followup_at == NOW + datetime.timedelta(days=7)


def test_reminders_wait_out_quiet_hours(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(
        quiet_hours_start=datetime.time(8, 0),
        quiet_hours_end=datetime.time(12, 0),
        notify_email=False,
    )  # 10:00 UTC is 13:00 in Nairobi, so use a window around it
    from apps.accounts.models import UserSettings

    UserSettings.objects.filter(user=user).update(
        quiet_hours_start=datetime.time(12, 0), quiet_hours_end=datetime.time(15, 0)
    )
    due_app(user)
    with time_machine.travel(NOW, tick=False):
        services.send_followup_reminders()
        row = Notification.objects.get()
        assert row.deliver_after == utc(2026, 3, 4, 12, 0)  # 15:00 in Nairobi
        assert notification_services.deliver(row.pk) == notification_services.NOT_DUE


def test_ghosted_suggestions_go_out_once(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    application = seed(
        user, make_kit_job(external_id="gh"), applied=NOW - datetime.timedelta(days=22)
    )
    with time_machine.travel(NOW, tick=False):
        first = services.send_followup_reminders()
    with time_machine.travel(NOW + datetime.timedelta(days=1), tick=False):
        second = services.send_followup_reminders()
    assert first["ghost_suggestions"] == 1 and second["ghost_suggestions"] == 0
    ghost = Notification.objects.filter(payload__what="ghost")
    assert ghost.count() == 1 and ghost.get().dedupe_key == f"ghost:{application.pk}:telegram"
    notification_services.deliver(ghost.get().pk, now=NOW + datetime.timedelta(days=2))
    assert "No reply from Globex Corp for 3 weeks" in tg.messages[-1]["text"]


def test_no_ghost_suggestion_before_21_days(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user(notify_email=False)
    seed(user, make_kit_job(external_id="g20"), applied=NOW - datetime.timedelta(days=20))
    with time_machine.travel(NOW, tick=False):
        assert services.send_followup_reminders()["ghost_suggestions"] == 0


def test_inactive_users_are_not_reminded(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    due_app(user)
    user.is_active = False
    user.save()
    with time_machine.travel(NOW, tick=False):
        assert services.send_followup_reminders() == {"reminders": 0, "ghost_suggestions": 0}


# --- stats ---------------------------------------------------------------------------------------------------------------------


def test_stats_on_seeded_data(tg, fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    ew = SourceFactory(display_name="ExpertWoka")
    track_a, track_b = TrackFactory(user=user, name="Support"), TrackFactory(user=user, name="Data")
    kit = Kit.objects.create(user=user, job=make_kit_job(external_id="kj"), status="ready")

    def make(external_id: str, stage: str, *, source: Any = ew, track: Any = track_a, with_kit: bool = False,
             reply_after_days: int | None = None, platform: str = "") -> Application:  # fmt: skip
        job = make_kit_job(external_id=external_id, source=source, company_platform=platform)
        application = seed(user, job, "applied", applied=NOW)
        Application.objects.filter(pk=application.pk).update(
            stage=stage, track=track, kit=kit if with_kit else None
        )
        if reply_after_days is not None:
            ApplicationEvent.objects.create(
                user=user, application=application, from_stage="applied", to_stage=stage,
                at=NOW + datetime.timedelta(days=reply_after_days),
            )  # fmt: skip
        return application

    make("a", "screening", reply_after_days=2, with_kit=True)
    make("b", "rejected", reply_after_days=4)
    make("c", "ghosted", track=track_b)
    make("d", "applied", track=track_b, with_kit=True)
    make("e", "withdrawn", source=None, track=track_b)
    make("f", "hired", reply_after_days=10, platform="micro1")
    make("g", "saved")
    Application.objects.filter(job__external_id="g").update(applied_at=None)

    result = stats.compute(user)
    assert (
        result.counts["screening"] == 1
        and result.counts["saved"] == 1
        and result.counts["ghosted"] == 1
    )
    assert (result.applied, result.responded, result.response_rate) == (
        6,
        3,
        50,
    )  # saved is not sent
    assert result.median_days_to_response == 4.0  # 2, 4 and 10 days
    by_source = {r.label: (r.applied, r.responded) for r in result.by_source}
    assert by_source == {"ExpertWoka": (4, 2), "Added by you": (1, 0), "micro1": (1, 1)}
    by_track = {r.label: (r.applied, r.responded, r.rate) for r in result.by_track}
    assert by_track == {"Support": (3, 3, 100), "Data": (3, 0, 0)}
    by_kit = {r.label: (r.applied, r.responded) for r in result.by_kit}
    assert by_kit == {"With a kit": (2, 1), "Without a kit": (4, 2)}


def test_stats_with_nothing_are_zero_not_an_error(tg) -> None:  # type: ignore[no-untyped-def]
    result = stats.compute(make_notif_user())
    assert (result.applied, result.responded, result.response_rate) == (0, 0, 0)
    assert result.median_days_to_response is None and result.by_source == [] and result.by_kit == []


def test_the_median_of_an_even_number_of_responses(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    for i, days in enumerate((1, 2, 4, 9)):
        application = seed(user, make_kit_job(external_id=f"m{i}"), applied=NOW)
        ApplicationEvent.objects.create(
            user=user, application=application, from_stage="applied", to_stage="interview",
            at=NOW + datetime.timedelta(days=days),
        )  # fmt: skip
    assert stats.compute(user).median_days_to_response == 3.0


def test_only_the_first_response_counts_for_the_median(tg) -> None:  # type: ignore[no-untyped-def]
    user = make_notif_user()
    application = seed(user, make_kit_job(external_id="first"), applied=NOW)
    for days, stage in ((3, "screening"), (9, "interview")):
        ApplicationEvent.objects.create(
            user=user, application=application, from_stage="", to_stage=stage,
            at=NOW + datetime.timedelta(days=days),
        )  # fmt: skip
    result = stats.compute(user)
    assert result.median_days_to_response == 3.0 and result.responded == 1


def test_stats_never_include_other_users_data(tg) -> None:  # type: ignore[no-untyped-def]
    mine, theirs = make_notif_user(), make_notif_user(chat_id="2", full_name="Other Person")
    seed(theirs, make_kit_job(external_id="theirs"), "offer")
    assert stats.compute(mine).applied == 0 and stats.compute(theirs).applied == 1
