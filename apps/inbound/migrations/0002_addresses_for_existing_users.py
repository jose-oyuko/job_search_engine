"""Give every existing user a forwarding address (new users get one at signup)."""

import secrets

from django.conf import settings
from django.db import migrations

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _token() -> str:
    return "u-" + "".join(secrets.choice(_ALPHABET) for _ in range(6))


def create_addresses(apps, schema_editor):  # type: ignore[no-untyped-def]
    user_model = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    address_model = apps.get_model("inbound", "InboundAddress")
    have = set(address_model.objects.values_list("user_id", flat=True))
    used = set(address_model.objects.values_list("token", flat=True))
    for user_id in user_model.objects.exclude(pk__in=have).values_list("pk", flat=True):
        token = _token()
        while token in used:
            token = _token()
        used.add(token)
        address_model.objects.create(user_id=user_id, token=token)


class Migration(migrations.Migration):
    dependencies = [("inbound", "0001_initial")]

    operations = [migrations.RunPython(create_addresses, migrations.RunPython.noop)]
