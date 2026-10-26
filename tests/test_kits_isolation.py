"""Multi-user isolation for kits and kit files (spec §16): user A never sees or touches user B's."""

from typing import Any

import pytest
from django.contrib.auth.models import AnonymousUser
from django.test import Client
from django.urls import reverse

from apps.kits import services
from apps.kits.exceptions import NotOwner
from apps.kits.models import Kit
from apps.llm.providers.fake import FakeProvider
from tests.factories import UserFactory
from tests.kit_helpers import good_cv, make_kit_job, make_kit_user, script_kit

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("pdf_fallback")]
HX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def two_users(fake_llm: FakeProvider) -> tuple[Client, Any, Any, Kit, Any]:
    """User A (logged in) with no kit; user B with a ready kit on a public job."""
    a, _ = make_kit_user(full_name="Alice Aardvark")
    b, b_ids = make_kit_user(full_name="Bob Buffalo")
    job = make_kit_job()
    script_kit(fake_llm, good_cv(b_ids))
    kit = services.request_kit(b, job)
    services.generate_kit(kit.pk)
    kit.refresh_from_db()
    client = Client()
    client.force_login(a)
    return client, a, b, kit, job


@pytest.mark.parametrize("key", ["cv_pdf", "cv_docx", "cover_letter_pdf", "cover_letter_docx"])
def test_other_users_files_cannot_be_downloaded(two_users, key: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, kit, _ = two_users
    assert client.get(reverse("kits:download", args=[kit.pk, key])).status_code == 404


def test_the_owner_can_download_but_anonymous_users_cannot(two_users) -> None:  # type: ignore[no-untyped-def]
    _, _, b, kit, _ = two_users
    owner = Client()
    owner.force_login(b)
    assert owner.get(reverse("kits:download", args=[kit.pk, "cv_pdf"])).status_code == 200
    response = Client().get(reverse("kits:download", args=[kit.pk, "cv_pdf"]))
    assert response.status_code == 302 and "/accounts/login/" in response["Location"]


@pytest.mark.parametrize("action", ["edit", "rerender"])
def test_other_users_kits_cannot_be_changed(two_users, action: str) -> None:  # type: ignore[no-untyped-def]
    client, _, _, kit, _ = two_users
    before = (kit.cv_data, kit.cover_letter_text, kit.cv_pdf.name, kit.user_edited)
    response = client.post(
        reverse(f"kits:{action}", args=[kit.pk]),
        {"headline": "Hacked", "cover_letter": "Hacked", "exp-0-0": "Hacked"},
        **HX,
    )
    assert response.status_code == 404
    kit.refresh_from_db()
    assert (kit.cv_data, kit.cover_letter_text, kit.cv_pdf.name, kit.user_edited) == before


def test_the_panel_never_shows_another_users_kit(two_users) -> None:  # type: ignore[no-untyped-def]
    client, _, _, kit, job = two_users
    html = client.get(f"/app/jobs/{job.pk}/").content.decode()
    assert "Bob Buffalo" not in html and "Resolved support tickets" not in html
    assert "Generate kit" in html and "All claims trace" not in html
    assert f"/app/kits/{kit.pk}/" not in html


def test_selecting_another_users_version_shows_nothing_of_it(two_users) -> None:  # type: ignore[no-untyped-def]
    client, _, _, kit, job = two_users
    html = client.get(reverse("kits:panel", args=[job.pk]) + f"?kit={kit.pk}").content.decode()
    assert "Resolved support tickets" not in html and "Generate kit" in html


def test_generating_for_a_job_does_not_touch_another_users_kit(two_users, fake_llm) -> None:  # type: ignore[no-untyped-def]
    client, a, _, kit, job = two_users
    response = client.post(reverse("kits:generate", args=[job.pk]), **HX)
    assert response.status_code == 200
    assert Kit.objects.filter(user=a, job=job).count() == 1
    assert Kit.objects.get(pk=kit.pk).version == 1  # B's numbering is independent of A's
    assert Kit.objects.get(user=a, job=job).version == 1


def test_another_users_private_job_is_not_found_for_the_panel_and_generation(two_users) -> None:  # type: ignore[no-untyped-def]
    client, _, b, _, _ = two_users
    private = make_kit_job(
        visibility="private", owner=b, source=None, origin="manual_text", external_id="p1"
    )
    assert client.get(reverse("kits:panel", args=[private.pk])).status_code == 404
    assert client.post(reverse("kits:generate", args=[private.pk]), **HX).status_code == 404
    assert client.get(f"/app/jobs/{private.pk}/").status_code == 404
    assert not Kit.objects.filter(job=private).exists()


def test_for_user_scopes_kits() -> None:
    a, _ = make_kit_user(full_name="A One")
    b, _ = make_kit_user(full_name="B Two")
    Kit.objects.create(user=a, job=make_kit_job(external_id="1"))
    Kit.objects.create(user=b, job=make_kit_job(external_id="2"))
    assert Kit.objects.for_user(a).count() == 1 and Kit.objects.for_user(a).get().user == a
    assert (
        Kit.objects.for_user(AnonymousUser()).count() == 0
        and Kit.objects.for_user(None).count() == 0
    )


def test_the_service_layer_refuses_foreign_kits_and_jobs(two_users) -> None:  # type: ignore[no-untyped-def]
    _, a, _, kit, _ = two_users
    with pytest.raises(NotOwner):
        services.get_kit(a, kit.pk)
    with pytest.raises(NotOwner):
        services.rerender(a, kit)
    with pytest.raises(NotOwner):
        services.apply_edits(a, kit, services.KitEdits(headline="x"))
    assert services.kit_versions(a, kit.job) == []
    assert services.latest_ready_kit(a, kit.job) is None


def test_a_kits_documents_use_only_its_owners_profile(fake_llm) -> None:  # type: ignore[no-untyped-def]
    """Even if a kit's data cited someone else's IDs, nothing of theirs reaches the files."""
    a, a_ids = make_kit_user(full_name="Alice Aardvark")
    b, b_ids = make_kit_user(full_name="Bob Buffalo")
    kit = Kit.objects.create(
        user=a, job=make_kit_job(), status=Kit.Status.READY, cv_data=good_cv(b_ids)
    )
    from apps.kits import documents

    ctx = documents.build_cv_context(kit)
    assert ctx["name"] == "Alice Aardvark" and ctx["experiences"] == []
    assert a_ids is not b_ids


def test_one_users_files_get_distinct_random_names_under_the_private_root(  # type: ignore[no-untyped-def]
    two_users, settings
) -> None:
    _, _, _, kit, _ = two_users
    names = {
        kit.cv_pdf.name,
        kit.cv_docx.name,
        kit.cover_letter_pdf.name,
        kit.cover_letter_docx.name,
    }
    assert len(names) == 4 and all(n.startswith("kits/") for n in names)
    assert all("Bob" not in n and "Buffalo" not in n for n in names)
    assert kit.cv_pdf.path.startswith(str(settings.PRIVATE_MEDIA_ROOT))


def test_the_admin_shows_no_document_text(two_users, admin_client) -> None:  # type: ignore[no-untyped-def]
    _, _, _, kit, _ = two_users
    page = admin_client.get(f"/admin/kits/kit/{kit.pk}/change/").content.decode()
    assert "Resolved support tickets" not in page and "Dear Hiring Team" not in page
    assert "cover_letter_text" not in page and "cv_data" not in page
    assert UserFactory  # keep the import honest
