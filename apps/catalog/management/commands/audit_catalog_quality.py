"""Management command: audit_catalog_quality (Phase 43).

Run automated catalog quality audit, verify data integrity, print
health metrics, and optionally perform safe remediation.
"""

from __future__ import annotations

import csv
import json
import sys

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.catalog.models import Product
from apps.catalog.quality import (
    CatalogIssueSeverity,
    audit_catalog,
    ensure_variant_stock,
    normalize_primary_images,
    populate_fallback_alt_text,
)


class Command(BaseCommand):
    help = "Audit catalog data quality, report consistency issues, and apply safe remediation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--severity",
            choices=["blocking", "warning", "recommendation"],
            help="Filter audit issues by severity.",
        )
        parser.add_argument(
            "--status",
            choices=["active", "draft", "archived"],
            help="Filter audit by product editorial status.",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Apply safe remediation: initialize missing Stock rows, normalize primary images, populate alt text.",
        )
        parser.add_argument(
            "--unpublish-broken",
            action="store_true",
            help="Safely unpublish active products with blocking errors (returns to Draft).",
        )
        parser.add_argument(
            "--export-csv",
            type=str,
            help="Export audit issues to specified CSV file path.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Output audit summary as JSON.",
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Exit with non-zero status code if any blocking errors are detected (useful for CI/CD gates).",
        )

    def handle(self, *args, **options):
        # 1. Apply remediation if requested
        if options["fix"]:
            self.stdout.write(self.style.NOTICE("Applying safe catalog remediation..."))
            stock_count = ensure_variant_stock()
            img_count = normalize_primary_images()
            alt_count = populate_fallback_alt_text()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Remediation complete: {stock_count} stock rows created, "
                    f"{img_count} primary images normalized, {alt_count} alt texts populated."
                )
            )

        if options["unpublish_broken"]:
            initial_report = audit_catalog(status=Product.Status.ACTIVE)
            blocking_pks = [
                pk
                for pk, audit in initial_report.product_audits.items()
                if audit.blocking_count > 0 and audit.product.status == Product.Status.ACTIVE
            ]
            if blocking_pks:
                now = timezone.now()
                updated = Product.objects.filter(pk__in=blocking_pks).update(
                    status=Product.Status.DRAFT, updated_at=now
                )
                self.stdout.write(
                    self.style.WARNING(
                        f"Unpublished {updated} active product(s) with blocking errors."
                    )
                )

        # 2. Run Audit
        severity = options.get("severity")
        status = options.get("status")
        report = audit_catalog(severity=severity, status=status)

        # 3. Export CSV if requested
        if options.get("export_csv"):
            filepath = options["export_csv"]
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "Product ID",
                        "Product Name",
                        "Severity",
                        "Category Area",
                        "Issue Code",
                        "Description",
                        "Variant SKU",
                    ]
                )
                for issue in report.all_issues:
                    writer.writerow(
                        [
                            issue.product_id,
                            issue.product_name,
                            issue.severity,
                            issue.category,
                            issue.code,
                            issue.message,
                            issue.variant_sku or "",
                        ]
                    )
            self.stdout.write(self.style.SUCCESS(f"Exported {len(report.all_issues)} issues to {filepath}"))

        # 4. Output results
        if options["json"]:
            data = {
                "total_products": report.total_products,
                "healthy_products_count": report.healthy_products_count,
                "health_index": report.health_index,
                "blocking_issues_count": report.blocking_issues_count,
                "warning_issues_count": report.warning_issues_count,
                "recommendation_issues_count": report.recommendation_issues_count,
                "issues_by_code": report.issues_by_code,
                "issues_by_severity": report.issues_by_severity,
                "issues_by_category": report.issues_by_category,
            }
            self.stdout.write(json.dumps(data, indent=2))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("=== FLASHWEAR Catalog Quality Audit ==="))
            self.stdout.write(f"Total Products:         {report.total_products}")
            self.stdout.write(f"Healthy & Publishable:  {report.healthy_products_count}")
            self.stdout.write(f"Overall Health Index:   {report.health_index}%")
            self.stdout.write("")
            self.stdout.write(self.style.ERROR(f"Blocking Errors:        {report.blocking_issues_count}"))
            self.stdout.write(self.style.WARNING(f"Warnings:               {report.warning_issues_count}"))
            self.stdout.write(self.style.NOTICE(f"Recommendations:        {report.recommendation_issues_count}"))
            self.stdout.write("")

            if report.issues_by_code:
                self.stdout.write(self.style.MIGRATE_LABEL("Issues by Code:"))
                for code, count in sorted(report.issues_by_code.items(), key=lambda x: -x[1]):
                    self.stdout.write(f"  - {code}: {count}")

        # 5. Strict CI gate check
        if options["strict"] and report.blocking_issues_count > 0:
            self.stderr.write(
                self.style.ERROR(
                    f"Audit failed: {report.blocking_issues_count} blocking catalog error(s) detected!"
                )
            )
            sys.exit(1)
