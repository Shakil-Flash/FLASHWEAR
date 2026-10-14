"""Initial migration for Staff Portal: seeds standard staff role groups."""

from django.db import migrations

STAFF_GROUPS = (
    "Owner",
    "Operations Manager",
    "Product Manager",
    "Inventory Staff",
    "Order Processing Staff",
    "Delivery Staff",
    "Support Staff",
    "Read-Only Staff",
)


def create_staff_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in STAFF_GROUPS:
        Group.objects.get_or_create(name=name)


def remove_staff_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=STAFF_GROUPS).delete()


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_staff_groups, remove_staff_groups),
    ]
