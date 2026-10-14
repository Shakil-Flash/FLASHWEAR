"""Tests for the reconcile_catalog_images management command."""

from io import StringIO
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command

from apps.catalog.models import Product, ProductImage


@pytest.mark.django_db
class TestReconcileCatalogImagesCommand:
    def test_reconcile_catalog_images_runs_cleanly(self):
        """Command runs without error on the test catalog."""
        out = StringIO()
        call_command("reconcile_catalog_images", stdout=out)
        output = out.getvalue()
        assert "Catalog images reconciled successfully!" in output

    def test_reconcile_catalog_images_prunes_missing_files(self, product):
        """DB records pointing to non-existent disk files are pruned."""
        broken = ProductImage.objects.create(
            product=product,
            image="catalog/products/99999/non_existent_image_file.jpg",
            alt_text="Broken record",
            position=99,
            is_primary=False,
        )
        assert ProductImage.objects.filter(pk=broken.pk).exists()

        out = StringIO()
        call_command("reconcile_catalog_images", stdout=out)
        output = out.getvalue()

        assert "[Prune] Missing file" in output
        assert not ProductImage.objects.filter(pk=broken.pk).exists()

    def test_reconcile_enforces_primary_image(self, product):
        """Every product ends up with exactly one primary image."""
        ProductImage.objects.filter(product=product).delete()

        out = StringIO()
        call_command("reconcile_catalog_images", stdout=out)

        images = ProductImage.objects.filter(product=product)
        assert images.count() >= 1
        assert images.filter(is_primary=True).count() == 1
