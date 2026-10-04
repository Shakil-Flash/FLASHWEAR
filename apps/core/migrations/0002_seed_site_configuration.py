"""Seed the singleton SiteConfiguration row.

Without this the context processor would need to write to the database on the first
request, which breaks read-only replicas and turns a render into a write.
"""

from django.db import migrations

SINGLETON_PK = 1


def create_site_configuration(apps, schema_editor):
    SiteConfiguration = apps.get_model("core", "SiteConfiguration")
    SiteConfiguration.objects.get_or_create(pk=SINGLETON_PK)


def noop(apps, schema_editor):
    """The row is intentionally left in place: templates depend on it existing."""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_site_configuration, noop),
    ]
