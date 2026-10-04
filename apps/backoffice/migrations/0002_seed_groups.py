"""Create the eight back-office groups (Phase 16).
The groups are the app's contract, not seed data: without them
:func:`apps.backoffice.permissions.has` answers False for every non-superuser, which is
the safe direction but makes a freshly migrated database look like the feature is
missing.

Support's two groups are created by ``support.0002`` and are deliberately not touched
here: one owner per row, so reversing one migration cannot strand the other app.

Reverse removes them, because a reversed migration should not leave role rows behind for
a user to be silently promoted by later.
"""

from django.db import migrations

BACKOFFICE_GROUPS = (
    "Back Office Operator",
    "Order Manager",
    "Inventory Manager",
    "Finance",
    "Marketing Manager",
    "Content Moderator",
    "Catalog Manager",
    "Administrator",
)


def create_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in BACKOFFICE_GROUPS:
        Group.objects.get_or_create(name=name)


def remove_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=BACKOFFICE_GROUPS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("backoffice", "0001_initial"),
        ("auth", "0001_initial"),
        ("support", "0002_seed_capabilities"),
    ]

    operations = [
        migrations.RunPython(create_groups, remove_groups),
    ]
