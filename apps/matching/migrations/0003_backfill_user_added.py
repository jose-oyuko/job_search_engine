"""A match that fails the rules can only exist because the user added the job themselves."""

from django.db import migrations


def backfill(apps, schema_editor):  # type: ignore[no-untyped-def]
    apps.get_model("matching", "Match").objects.filter(rule_pass=False).update(user_added=True)


class Migration(migrations.Migration):
    dependencies = [("matching", "0002_match_user_added")]

    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
