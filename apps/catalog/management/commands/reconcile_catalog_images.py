"""Management command: reconcile_catalog_images.

Synchronizes the database's ProductImage records with the actual media files present
on disk in settings.MEDIA_ROOT / "catalog" / "products".

Why this exists:
1. In containerized environments with ephemeral storage (e.g. Render Free Tier),
   media files committed to Git are baked into the container image, but the external
   PostgreSQL database may contain stale image records pointing to files generated
   during earlier sessions that no longer exist on disk.
2. Conversely, files present in Git/Docker might not have corresponding records in a
   freshly migrated or disconnected database.
3. This command reconciles disk and database deterministically:
   - Removes orphaned ProductImage rows that point to missing files.
   - Syncs canonical image definitions and attributes from fixtures.
   - Discovers any additional product images present on disk and attaches them.
   - Ensures every product has exactly one valid primary image.
   - Generates a lightweight placeholder if any product has no images at all.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.catalog.models import Color, Product, ProductImage

VALID_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


class Command(BaseCommand):
    help = "Reconcile catalog ProductImage database records with actual media files on disk."

    def add_arguments(self, parser):
        parser.add_argument(
            "--quiet",
            action="store_true",
            help="Suppress informational output.",
        )
        parser.add_argument(
            "--no-prune",
            action="store_true",
            help="Do not delete database records whose files are missing from disk.",
        )

    def handle(self, *args, **options):
        quiet = options.get("quiet", False)
        no_prune = options.get("no_prune", False)

        media_root = Path(settings.MEDIA_ROOT)
        products_dir = media_root / "catalog" / "products"
        products_dir.mkdir(parents=True, exist_ok=True)

        if not quiet:
            self.stdout.write(self.style.NOTICE(f"==> Reconciling catalog images (MEDIA_ROOT: {media_root})..."))

        pruned_count = 0
        synced_count = 0
        discovered_count = 0
        placeholder_count = 0

        with transaction.atomic():
            # ------------------------------------------------------------------
            # Phase 1: Prune broken / orphaned database records
            # ------------------------------------------------------------------
            if not no_prune:
                for img in ProductImage.objects.select_related("product").all():
                    if not img.image or not img.image.name:
                        img.delete()
                        pruned_count += 1
                        continue

                    full_path = media_root / img.image.name
                    if not full_path.exists():
                        if not quiet:
                            self.stdout.write(
                                self.style.WARNING(
                                    f"  [Prune] Missing file for product {img.product_id}: {img.image.name}"
                                )
                            )
                        img.delete()
                        pruned_count += 1

            # ------------------------------------------------------------------
            # Phase 2: Load canonical images from fixture if present
            # ------------------------------------------------------------------
            fixture_path = (
                Path(__file__).resolve().parent.parent.parent
                / "fixtures"
                / "canonical_product_images.json"
            )
            if fixture_path.exists():
                try:
                    with open(fixture_path, "r", encoding="utf-8") as f:
                        records = json.load(f)

                    for rec in records:
                        if rec.get("model") != "catalog.productimage":
                            continue
                        fields = rec.get("fields", {})
                        rel_img = fields.get("image")
                        product_id = fields.get("product")
                        if not rel_img or not product_id:
                            continue

                        # Verify that the file exists on disk before inserting
                        file_on_disk = media_root / rel_img
                        if not file_on_disk.exists():
                            continue

                        product = Product.objects.filter(pk=product_id).first()
                        if not product:
                            continue

                        _, created = ProductImage.objects.update_or_create(
                            product=product,
                            image=rel_img,
                            defaults={
                                "alt_text": fields.get("alt_text") or f"{product.name} photograph",
                                "position": fields.get("position", 0),
                                "is_primary": fields.get("is_primary", False),
                                "source_url": fields.get("source_url") or "",
                                "photographer": fields.get("photographer", ""),
                                "attribution": fields.get("attribution", ""),
                                "license": fields.get("license", ""),
                            },
                        )
                        synced_count += 1
                except Exception as err:
                    self.stderr.write(self.style.ERROR(f"Failed loading canonical fixture: {err}"))

            # ------------------------------------------------------------------
            # Phase 3: Filesystem discovery of any files in media/catalog/products/<id>/
            # ------------------------------------------------------------------
            if products_dir.exists():
                for entry in products_dir.iterdir():
                    if not entry.is_dir() or not entry.name.isdigit():
                        continue
                    prod_id = int(entry.name)
                    product = Product.objects.filter(pk=prod_id).first()
                    if not product:
                        continue

                    for file_path in sorted(entry.iterdir()):
                        if not file_path.is_file() or file_path.suffix.lower() not in VALID_IMAGE_EXTENSIONS:
                            continue
                        rel_path = f"catalog/products/{prod_id}/{file_path.name}"
                        if not ProductImage.objects.filter(product=product, image=rel_path).exists():
                            has_primary = ProductImage.objects.filter(product=product, is_primary=True).exists()
                            ProductImage.objects.create(
                                product=product,
                                image=rel_path,
                                alt_text=f"{product.name} image",
                                position=0,
                                is_primary=not has_primary,
                            )
                            discovered_count += 1

            # ------------------------------------------------------------------
            # Phase 4: Ensure every product has exactly one primary image
            # ------------------------------------------------------------------
            for product in Product.objects.all():
                images = list(ProductImage.objects.filter(product=product).order_by("position", "id"))
                if not images:
                    # Create a fallback placeholder image on disk
                    prod_folder = products_dir / str(product.id)
                    prod_folder.mkdir(parents=True, exist_ok=True)
                    placeholder_filename = "placeholder.png"
                    placeholder_disk_path = prod_folder / placeholder_filename

                    color = Color.objects.filter(
                        pk__in=product.variants.values_list("color_id", flat=True)
                    ).first()
                    hex_code = color.hex_code if color else "#e2e8f0"
                    _generate_solid_png(placeholder_disk_path, hex_code=hex_code)

                    rel_path = f"catalog/products/{product.id}/{placeholder_filename}"
                    ProductImage.objects.create(
                        product=product,
                        image=rel_path,
                        alt_text=f"{product.name} Placeholder",
                        position=0,
                        is_primary=True,
                    )
                    placeholder_count += 1
                else:
                    primaries = [img for img in images if img.is_primary]
                    if len(primaries) == 0:
                        first = images[0]
                        first.is_primary = True
                        first.save(update_fields=["is_primary", "updated_at"])
                    elif len(primaries) > 1:
                        # Demote all except the first
                        for img in primaries[1:]:
                            img.is_primary = False
                            img.save(update_fields=["is_primary", "updated_at"])

        if not quiet:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Catalog images reconciled successfully!\n"
                    f"  - Pruned broken records: {pruned_count}\n"
                    f"  - Canonical images synced: {synced_count}\n"
                    f"  - Disk images discovered: {discovered_count}\n"
                    f"  - Fallback placeholders created: {placeholder_count}\n"
                    f"  - Total products verified: {Product.objects.count()}"
                )
            )


def _generate_solid_png(target_path: Path, hex_code: str = "#e2e8f0", width: int = 1200, height: int = 1500) -> None:
    """Generate a solid placeholder PNG with subtle watermark stripes."""
    try:
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (width, height), hex_code)
        draw = ImageDraw.Draw(image)
        for offset in range(-height, width, 40):
            draw.line([(offset, height), (offset + height, 0)], fill="#ffffff", width=1)
        image.save(target_path, format="PNG")
    except Exception:
        # Fallback minimal 1x1 transparent PNG if Pillow is unavailable
        minimal_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff"
            b"?\x00\x05\xfe\x02\xfeA\xa1\xa9\x86\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        target_path.write_bytes(minimal_png)
