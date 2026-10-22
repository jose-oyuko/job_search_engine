import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import Http404

from apps.core.tenancy import get_owned_or_404
from tests.factories import UserFactory
from tests.testapp.models import Note

pytestmark = pytest.mark.django_db


def test_for_user_returns_only_own_rows() -> None:
    alice, bob = UserFactory(), UserFactory()
    mine = Note.objects.create(user=alice, text="mine")
    Note.objects.create(user=bob, text="theirs")

    assert list(Note.objects.for_user(alice)) == [mine]


def test_for_user_anonymous_and_none_get_nothing() -> None:
    Note.objects.create(user=UserFactory(), text="x")
    assert Note.objects.for_user(AnonymousUser()).count() == 0
    assert Note.objects.for_user(None).count() == 0


def test_get_owned_or_404_hides_other_users_rows() -> None:
    alice, bob = UserFactory(), UserFactory()
    note = Note.objects.create(user=alice, text="mine")

    assert get_owned_or_404(Note, alice, pk=note.pk) == note
    with pytest.raises(Http404):
        get_owned_or_404(Note, bob, pk=note.pk)


def test_timestamps_are_set() -> None:
    note = Note.objects.create(user=UserFactory(), text="t")
    assert note.created_at is not None
    assert note.updated_at >= note.created_at
