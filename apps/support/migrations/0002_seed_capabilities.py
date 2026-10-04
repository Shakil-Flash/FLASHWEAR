"""Create the two support capability groups (Phase 15).

The groups are part of the app's contract, not seed data: without them there is no
"Support Agent" to put a user in, and :func:`apps.support.permissions.has_capability`
answers False for everyone (which is the safe direction, but it also makes a freshly
migrated database look like the feature is missing).

Reverse removes them, because a reversed migration should not leave role rows behind for a
user to be silently promoted by later.
"""

from django.db import migrations

AGENT_GROUP = "Support Agent"
MANAGER_GROUP = "Support Manager"


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in (AGENT_GROUP, MANAGER_GROUP):
        Group.objects.get_or_create(name=name)


def remove_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=(AGENT_GROUP, MANAGER_GROUP)).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0001_initial"),
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
