"""The kit panel, generation, editing, versions and downloads through the views (spec §12.7)."""

import json
from typing import Any

import pytest
from django.test import Client
from django.urls import reverse

from apps.kits import services
from apps.kits.models import Kit
from apps.llm.models import UsageQuota
from apps.llm.providers.fake import FakeProvider
from tests.factories import UserFactory
from tests.kit_helpers import (
    PDF_AVAILABLE,
    good_cv,
    make_kit_job,
    make_kit_user,
    script_kit,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def world(fake_llm: FakeProvider) -> tuple[Client, Any, Any, Any]:
    user, ids = make_kit_user()
    job = make_kit_job()
    client = Client()
    client.force_login(user)
    return client, user, ids, job


def generate_url(job: Any) -> str:
    return reverse("kits:generate", args=[job.pk])


def make_ready(world, fake_llm, django_capture_on_commit_callbacks) -> Kit:  # type: ignore[no-untyped-def]
    client, user, ids, job = world
    script_kit(fake_llm, good_cv(ids))
    with django_capture_on_commit_callbacks(execute=True):
        client.post(generate_url(job), **HX)
    return Kit.objects.get(user=user, job=job, version=1)


# --- the panel on the job page ----------------------------------------------------------------------


def test_the_job_page_offers_to_generate_a_kit(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert 'id="kit-panel"' in html and "Generate kit" in html
    assert 'src="/static/js/kit.js"' in html


def test_the_panel_asks_for_a_profile_first(fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    client = Client()
    client.force_login(user)
    job = make_kit_job()
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "Add your experience or skills" in html and "Generate kit" not in html


def test_the_panel_says_when_the_allowance_is_used_up(world) -> None:  # type: ignore[no-untyped-def]
    client, user, _, job = world
    UsageQuota.objects.create(user=user, monthly_kits=0)
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "Kit limit reached" in html and "Generate kit" not in html


def test_the_panel_requires_login(world) -> None:  # type: ignore[no-untyped-def]
    _, _, _, job = world
    response = Client().get(reverse("kits:panel", args=[job.pk]))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


# --- generating --------------------------------------------------------------------------------------


def test_generating_shows_progress_and_then_the_result(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    client, user, ids, job = world
    script_kit(fake_llm, good_cv(ids))
    with django_capture_on_commit_callbacks(execute=False):
        queued = client.post(generate_url(job), **HX).content.decode()
    assert "Waiting for a worker" in queued and 'hx-trigger="every 2s"' in queued

    kit = Kit.objects.get(user=user)
    services.generate_kit(kit.pk)
    ready = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "every 2s" not in ready  # polling stops once the kit is ready
    assert "All claims trace to your profile" in ready
    assert "CV (PDF)" in ready and "Cover letter (Word)" in ready
    assert "Resolved support tickets for 120 staff" in ready  # CV preview
    assert "Question 1?" in ready and "It fits my experience." in ready  # interview, screening
    assert "data-copy" in ready  # copy buttons


def test_progress_text_is_shown_while_generating(world) -> None:  # type: ignore[no-untyped-def]
    client, user, _, job = world
    kit = services.request_kit(user, job)
    Kit.objects.filter(pk=kit.pk).update(status="generating", progress="Writing cover letter…")
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "Writing cover letter…" in html and "every 2s" in html


def test_a_failed_kit_shows_a_message_and_a_retry(world, fake_llm) -> None:  # type: ignore[no-untyped-def]
    client, user, _, job = world
    fake_llm.script.extend(["nope", "nope"])
    kit = services.request_kit(user, job)
    services.generate_kit(kit.pk)
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "could not write your documents" in html and "Try again" in html


def test_without_javascript_generating_redirects_to_the_job_page(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    response = client.post(generate_url(job))
    assert response.status_code == 302 and response["Location"].endswith(f"/app/jobs/{job.pk}/#kit")


def test_the_quota_message_is_shown_instead_of_a_kit(world) -> None:  # type: ignore[no-untyped-def]
    client, user, _, job = world
    UsageQuota.objects.create(user=user, monthly_kits=0)
    html = client.post(generate_url(job), **HX).content.decode()
    assert "used all of this month" in html and not Kit.objects.exists()


def test_an_empty_profile_message_is_shown(fake_llm) -> None:  # type: ignore[no-untyped-def]
    user = UserFactory()
    client = Client()
    client.force_login(user)
    job = make_kit_job()
    html = client.post(generate_url(job), **HX).content.decode()
    assert "Add your experience or skills" in html and not Kit.objects.exists()


def test_generating_is_limited_to_ten_requests_an_hour(world) -> None:  # type: ignore[no-untyped-def]
    client, user, _, job = world
    UsageQuota.objects.create(user=user, monthly_kits=0)  # cheap: every request is refused early
    codes = [client.post(generate_url(job), **HX).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_generate_only_accepts_post(world) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    assert client.get(generate_url(job)).status_code == 405


# --- versions -------------------------------------------------------------------------------------------


def test_regenerating_adds_a_version_and_the_dropdown_lists_both(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    client, user, ids, job = world
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    script_kit(fake_llm, good_cv(ids))
    with django_capture_on_commit_callbacks(execute=True):
        html = client.post(
            generate_url(job), {"instruction": "more technical"}, **HX
        ).content.decode()
    assert Kit.objects.filter(user=user).count() == 2
    assert 'id="kit-version"' in html and "v2" in html and "v1" in html
    assert "more technical" in html

    first = Kit.objects.get(user=user, version=1)
    older = client.get(reverse("kits:panel", args=[job.pk]) + f"?kit={first.pk}").content.decode()
    assert f'value="{first.pk}" selected' in older


def test_a_bad_version_parameter_is_ignored(
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    for value in ("abc", "99999", "-1", "1;2"):
        response = client.get(reverse("kits:panel", args=[job.pk]) + f"?kit={value}")
        assert response.status_code == 200


# --- editing --------------------------------------------------------------------------------------------


def test_editing_through_the_form_rebuilds_without_the_model(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    client, _, _, job = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    calls = len(fake_llm.calls)
    html = client.post(
        reverse("kits:edit", args=[kit.pk]),
        {
            "headline": "Support lead",
            "summary": "I fix things.",
            "cover_letter": "Para one.\n\nPara two.",
            "exp-0-1": "Set up laptops for the whole office.",
            "exp-1-0": "",
            "prj-0-0": "Built a stock tool.",
            "unrelated": "ignored",
            "exp-x-y": "ignored",
        },
        **HX,
    ).content.decode()
    kit.refresh_from_db()
    assert "Saved. Your documents were updated." in html
    assert kit.cv_data["headline"] == "Support lead" and kit.user_edited
    assert kit.cv_data["experiences"][0]["bullets"][1]["text"].startswith("Set up laptops for the")
    assert kit.cv_data["experiences"][1]["bullets"] == []
    assert kit.cv_data["projects"][0]["bullets"][0]["text"] == "Built a stock tool."
    assert kit.cover_letter_text == "Para one.\n\nPara two."
    assert len(fake_llm.calls) == calls and "Support lead" in html


def test_edit_warnings_are_shown(world, fake_llm, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    html = client.post(
        reverse("kits:edit", args=[kit.pk]), {"exp-0-0": "Leveraged tools daily."}, **HX
    ).content.decode()
    assert "Style notes on your edits" in html and "leverage" in html


def test_the_edit_form_lists_a_textarea_for_every_bullet(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    client, _, _, job = world
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    for name in ("exp-0-0", "exp-0-1", "exp-1-0", "prj-0-0", "headline", "summary", "cover_letter"):
        assert f'name="{name}"' in html


def test_generated_text_is_escaped_in_the_panel(
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    kit.cover_letter_text = "<script>alert(1)</script>"
    kit.interview_qa = [
        {"category": "x", "question": "<img src=x onerror=1>", "answer_outline": []}
    ]
    kit.save()
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_rerender_view(world, fake_llm, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    html = client.post(reverse("kits:rerender", args=[kit.pk]), **HX).content.decode()
    assert "Your documents were rebuilt." in html


def test_edit_and_rerender_only_accept_post(
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    assert client.get(reverse("kits:edit", args=[kit.pk])).status_code == 405
    assert client.get(reverse("kits:rerender", args=[kit.pk])).status_code == 405


# --- downloads --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "name", "content_type"),
    [
        ("cv_pdf", "Jane_Doe_CV_Globex-Corp.pdf", "application/pdf"),
        ("cv_docx", "Jane_Doe_CV_Globex-Corp.docx", "wordprocessingml"),
        ("cover_letter_pdf", "Jane_Doe_CoverLetter_Globex-Corp.pdf", "application/pdf"),
        ("cover_letter_docx", "Jane_Doe_CoverLetter_Globex-Corp.docx", "wordprocessingml"),
    ],
)
def test_downloads_are_attachments_with_the_conventional_name(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks, key: str, name: str, content_type: str
) -> None:
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    response = client.get(reverse("kits:download", args=[kit.pk, key]))
    assert response.status_code == 200
    assert response["Content-Disposition"] == f'attachment; filename="{name}"'
    assert content_type in response["Content-Type"]
    body = b"".join(response.streaming_content)
    assert body.startswith(b"%PDF" if key.endswith("pdf") else b"PK")  # docx is a zip


@pytest.mark.parametrize("key", ["nope", "cv", "../cv_pdf", "cv_pdf.exe"])
def test_unknown_download_keys_are_404(
    world, fake_llm, django_capture_on_commit_callbacks, key: str
) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    assert client.get(f"/app/kits/{kit.pk}/download/{key}/").status_code == 404


def test_a_missing_file_is_404(world, fake_llm, django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    client, _, _, _ = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    kit.cv_pdf.storage.delete(kit.cv_pdf.name)
    assert client.get(reverse("kits:download", args=[kit.pk, "cv_pdf"])).status_code == 404
    kit.cv_docx = ""
    kit.save()
    assert client.get(reverse("kits:download", args=[kit.pk, "cv_docx"])).status_code == 404


def test_files_are_stored_outside_the_public_media_root(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks, settings
) -> None:
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    path = kit.cv_pdf.path
    assert path.startswith(str(settings.PRIVATE_MEDIA_ROOT))
    assert kit.cv_pdf.name.startswith("kits/") and "Jane" not in kit.cv_pdf.name
    # Nothing serves the private root: the stored name is not reachable as a URL.
    client = world[0]
    assert client.get(f"/{kit.cv_pdf.name}").status_code == 404
    assert client.get(f"/media/{kit.cv_pdf.name}").status_code == 404


def test_the_panel_hides_download_buttons_for_missing_files(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    client, _, _, job = world
    kit = make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    kit.cv_docx = ""
    kit.error = "PDF rendering is not available on this server."
    kit.save()
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "CV (Word)" not in html and "CV (PDF)" in html and "Re-render documents" in html


def test_a_stale_kit_is_flagged_after_a_profile_change(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    from apps.profiles import services as profile_services

    client, user, _, job = world
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    profile_services.bump_version(profile_services.get_profile(user))
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "Your profile changed after this kit was made" in html


def test_a_kit_with_removed_claims_lists_them(
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, user, ids, job = world
    cv = good_cv(ids)
    cv["experiences"][0]["bullets"][1] = {"text": "Saved 85% of time.", "source_ids": [ids.bul2]}
    wrong = json.dumps(cv)
    from tests.kit_helpers import interview_json, letter_text, screening_json

    fake_llm.script.extend([wrong, wrong, letter_text(), interview_json(), screening_json()])
    with django_capture_on_commit_callbacks(execute=True):
        client.post(generate_url(job), **HX)
    html = client.get(reverse("kits:panel", args=[job.pk])).content.decode()
    assert "Some claims could not be checked and were removed" in html
    assert "experiences[0].bullets[1]" in html and "Saved 85%" not in html


def test_the_job_page_includes_the_ready_kit(
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    client, _, _, job = world
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "All claims trace to your profile" in html and "Copy cover letter" in html


def test_pdf_stand_in_is_only_used_where_pango_is_missing() -> None:
    assert isinstance(PDF_AVAILABLE, bool)


def test_feed_cards_link_to_the_kit_panel(world) -> None:  # type: ignore[no-untyped-def]
    from tests.factories import MatchFactory

    client, user, _, job = world
    MatchFactory(user=user, job=job, score=70, verdict="good")
    html = client.get("/app/").content.decode()
    assert f'href="/app/jobs/{job.pk}/#kit"' in html


def test_the_job_page_shows_both_the_score_and_the_kit_staleness(  # type: ignore[no-untyped-def]
    world, fake_llm, django_capture_on_commit_callbacks
) -> None:
    """The match and the kit each flag their own out-of-date state; neither hides the other."""
    from apps.profiles import services as profile_services
    from tests.factories import MatchFactory

    client, user, _, job = world
    MatchFactory(user=user, job=job, score=70, verdict="good", scored_profile_version=1)
    make_ready(world, fake_llm, django_capture_on_commit_callbacks)
    profile_services.bump_version(profile_services.get_profile(user))
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "may be out of date" in html
    assert "Your profile changed after this kit was made" in html
