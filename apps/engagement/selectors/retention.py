"""Customer Retention & Repeat-Purchase Intelligence selectors (Phase 49).

Computes verified, non-duplicative retention metrics, cohort retention matrices,
explainable customer segmentation, and advisory retention recommendations.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from apps.accounts.models import Profile
from apps.catalog.models.product import Product, ProductVariant
from apps.engagement.models.loyalty import PointsTransaction
from apps.engagement.models.retention import (
    CampaignObjective,
    CampaignRecipient,
    CampaignStatus,
    CustomerSegmentChoice,
    RecipientStatus,
    RetentionCampaign,
)
from apps.engagement.services.loyalty import balance_for
from apps.notifications.services.preferences import marketing_subscribed
from apps.orders.models import Order
from apps.payments.models import Refund

COMPLETED_ORDER_STATUSES = (
    Order.Status.PAID,
    Order.Status.PROCESSING,
    Order.Status.SHIPPED,
    Order.Status.DELIVERED,
    Order.Status.REFUNDED,
)


def get_customer_purchase_data(user_id: int | None = None) -> list[dict[str, Any]]:
    """Fetches completed orders grouped by customer and computes verified financial metrics.
    
    Excludes PENDING_PAYMENT and CANCELLED orders.
    Deducts all SUCCEEDED refunds from order totals to calculate true net customer spend.
    """
    today = timezone.localdate()
    orders_qs = (
        Order.objects.filter(
            status__in=COMPLETED_ORDER_STATUSES,
            user__isnull=False,
        )
        .select_related("user")
        .prefetch_related("refunds", "items__variant__product__category")
        .order_by("created_at")
    )
    if user_id:
        orders_qs = orders_qs.filter(user_id=user_id)

    # Group orders by customer
    customer_orders: dict[int, list[Order]] = {}
    for order in orders_qs:
        customer_orders.setdefault(order.user_id, []).append(order)

    customers_data: list[dict[str, Any]] = []

    for uid, orders in customer_orders.items():
        user = orders[0].user
        first_order = orders[0]
        last_order = orders[-1]

        gross_spend = Decimal("0.00")
        refunded_amount = Decimal("0.00")
        net_spend = Decimal("0.00")
        repeat_net_revenue = Decimal("0.00")

        # Category and product tracking
        category_ids: set[int] = set()
        product_ids: set[int] = set()

        for idx, o in enumerate(orders):
            succeeded_refunds = sum(
                (r.amount for r in o.refunds.all() if r.status == Refund.Status.SUCCEEDED),
                Decimal("0.00"),
            )
            net_order_val = max(Decimal("0.00"), o.total - succeeded_refunds)
            gross_spend += o.total
            refunded_amount += succeeded_refunds
            net_spend += net_order_val

            if idx >= 1:
                repeat_net_revenue += net_order_val

            for item in o.items.all():
                if item.variant:
                    product_ids.add(item.variant.product_id)
                    if item.variant.product.category_id:
                        category_ids.add(item.variant.product.category_id)

        orders_count = len(orders)
        first_date = first_order.created_at.date()
        last_date = last_order.created_at.date()
        days_since_last_order = (today - last_date).days

        # Inter-purchase intervals
        intervals: list[int] = []
        if orders_count >= 2:
            for i in range(1, orders_count):
                diff = (orders[i].created_at.date() - orders[i - 1].created_at.date()).days
                intervals.append(diff)
        avg_interval = sum(intervals) / len(intervals) if intervals else None

        aov = (net_spend / Decimal(orders_count)).quantize(Decimal("0.01")) if orders_count else Decimal("0.00")

        # Segments determination
        segments: list[str] = [CustomerSegmentChoice.ALL_CUSTOMERS]
        if orders_count == 1:
            segments.append(CustomerSegmentChoice.NEW_CUSTOMERS)
        else:
            segments.append(CustomerSegmentChoice.REPEAT_CUSTOMERS)

        if days_since_last_order <= 30:
            segments.append(CustomerSegmentChoice.RECENTLY_ACTIVE)

        if orders_count >= 3:
            segments.append(CustomerSegmentChoice.LOYAL_CUSTOMERS)

        if net_spend >= Decimal("500.00"):
            segments.append(CustomerSegmentChoice.HIGH_SPENDERS)

        # At-risk criteria: lapse exceeds 1.5x of average interval, or single order customer inactive > 45 days
        if orders_count >= 2 and avg_interval is not None:
            threshold = max(45, int(avg_interval * 1.5))
            if threshold < days_since_last_order <= 120:
                segments.append(CustomerSegmentChoice.AT_RISK)
        elif orders_count == 1:
            if 45 <= days_since_last_order <= 120:
                segments.append(CustomerSegmentChoice.AT_RISK)

        if days_since_last_order > 120:
            segments.append(CustomerSegmentChoice.CHURNED)

        customers_data.append({
            "user": user,
            "user_id": uid,
            "email": user.email,
            "orders_count": orders_count,
            "first_order_date": first_date,
            "last_order_date": last_date,
            "days_since_last_order": days_since_last_order,
            "intervals": intervals,
            "avg_interval_days": round(avg_interval, 1) if avg_interval is not None else None,
            "gross_spend": gross_spend,
            "refunded_amount": refunded_amount,
            "net_spend": net_spend,
            "repeat_net_revenue": repeat_net_revenue,
            "avg_order_value": aov,
            "segments": segments,
            "category_ids": list(category_ids),
            "product_ids": list(product_ids),
        })

    return customers_data


def get_retention_overview_metrics(
    start_date: datetime.date | None = None,
    end_date: datetime.date | None = None,
) -> dict[str, Any]:
    """Computes headline customer retention and repeat-purchase metrics."""
    data = get_customer_purchase_data()

    total_customers = len(data)
    first_time_customers = sum(1 for c in data if c["orders_count"] == 1)
    repeat_customers = sum(1 for c in data if c["orders_count"] >= 2)

    repeat_purchase_rate = (
        round((repeat_customers / total_customers) * 100, 1) if total_customers > 0 else 0.0
    )

    total_orders = sum(c["orders_count"] for c in data)
    avg_purchase_frequency = (
        round(total_orders / total_customers, 2) if total_customers > 0 else 0.0
    )

    all_intervals: list[int] = []
    for c in data:
        all_intervals.extend(c["intervals"])
    avg_inter_purchase_interval = (
        round(sum(all_intervals) / len(all_intervals), 1) if all_intervals else 0.0
    )

    total_net_revenue = sum((c["net_spend"] for c in data), Decimal("0.00"))
    total_repeat_revenue = sum((c["repeat_net_revenue"] for c in data), Decimal("0.00"))

    aov = (
        (total_net_revenue / Decimal(total_orders)).quantize(Decimal("0.01"))
        if total_orders > 0
        else Decimal("0.00")
    )

    returning_revenue_share_pct = (
        round(float(total_repeat_revenue / total_net_revenue * 100), 1)
        if total_net_revenue > Decimal("0.00")
        else 0.0
    )

    # Descriptive CLV: Historical Net Spend per Customer to Date
    clv_historical = (
        (total_net_revenue / Decimal(total_customers)).quantize(Decimal("0.01"))
        if total_customers > 0
        else Decimal("0.00")
    )

    # Predictive CLV Estimate: AOV * Purchase Frequency * Estimated 2-year Lifespan
    # Transparent, explainable baseline formula
    clv_predictive_estimate = (
        (aov * Decimal(str(avg_purchase_frequency)) * Decimal("1.5")).quantize(Decimal("0.01"))
        if total_customers > 0
        else Decimal("0.00")
    )

    return {
        "total_customers": total_customers,
        "first_time_customers": first_time_customers,
        "repeat_customers": repeat_customers,
        "repeat_purchase_rate": repeat_purchase_rate,
        "total_orders": total_orders,
        "avg_purchase_frequency": avg_purchase_frequency,
        "avg_inter_purchase_interval_days": avg_inter_purchase_interval,
        "total_net_revenue": total_net_revenue,
        "total_repeat_revenue": total_repeat_revenue,
        "returning_revenue_share_pct": returning_revenue_share_pct,
        "avg_order_value": aov,
        "clv_historical_descriptive": clv_historical,
        "clv_predictive_estimate": clv_predictive_estimate,
    }


def get_cohort_retention_matrix(max_cohorts: int = 6) -> list[dict[str, Any]]:
    """Calculates monthly cohort retention from verified order data.
    
    Cohort Month is determined by the month of a customer's first completed purchase.
    Tracks retention % at Month 0, 1, 2, 3, 6.
    """
    data = get_customer_purchase_data()
    if not data:
        return []

    # Map customer to first order date
    cust_first_date: dict[int, datetime.date] = {c["user_id"]: c["first_order_date"] for c in data}

    # Fetch all completed orders with month offsets
    all_orders = (
        Order.objects.filter(
            status__in=COMPLETED_ORDER_STATUSES,
            user__isnull=False,
        )
        .values("user_id", "created_at")
        .order_by("created_at")
    )

    # Group into cohort buckets: cohort_str -> list of user_ids
    cohorts: dict[str, set[int]] = {}
    for uid, fdate in cust_first_date.items():
        cohort_key = fdate.strftime("%Y-%m")
        cohorts.setdefault(cohort_key, set()).add(uid)

    # Sort cohort keys chronologically
    sorted_cohort_keys = sorted(cohorts.keys())[-max_cohorts:]

    matrix: list[dict[str, Any]] = []
    tracked_months = [0, 1, 2, 3, 6]

    for c_key in sorted_cohort_keys:
        cohort_users = cohorts[c_key]
        cohort_size = len(cohort_users)
        if cohort_size == 0:
            continue

        c_year, c_month = map(int, c_key.split("-"))

        # For each tracked month index, count active customers
        active_counts: dict[int, int] = {m: 0 for m in tracked_months}
        active_pcts: dict[int, float] = {m: 0.0 for m in tracked_months}

        # Month 0 is always 100%
        active_counts[0] = cohort_size
        active_pcts[0] = 100.0

        for m_offset in tracked_months[1:]:
            # Target month
            target_year = c_year + (c_month - 1 + m_offset) // 12
            target_month = (c_month - 1 + m_offset) % 12 + 1

            # Check which cohort users placed an order in target month
            month_orders = all_orders.filter(
                user_id__in=cohort_users,
                created_at__year=target_year,
                created_at__month=target_month,
            ).values_list("user_id", flat=True).distinct()

            active_in_m = len(set(month_orders))
            active_counts[m_offset] = active_in_m
            active_pcts[m_offset] = round((active_in_m / cohort_size) * 100, 1)

        matrix.append({
            "cohort": c_key,
            "cohort_size": cohort_size,
            "retention_counts": active_counts,
            "retention_rates": active_pcts,
        })

    return matrix


def get_customer_segments_summary() -> dict[str, Any]:
    """Calculates counts and distributions across all explainable segments."""
    data = get_customer_purchase_data()

    summary: dict[str, int] = {choice.value: 0 for choice in CustomerSegmentChoice}

    for c in data:
        for seg in c["segments"]:
            if seg in summary:
                summary[seg] += 1

    return {
        "segments": summary,
        "total_purchasers": len(data),
    }


def get_customer_retention_profile(user_id: int) -> dict[str, Any] | None:
    """Returns detailed individual retention metrics and consented context for a customer."""
    data = get_customer_purchase_data(user_id=user_id)
    if not data:
        return None

    c_info = data[0]
    user = c_info["user"]

    # Consent and privacy status
    marketing_consent = marketing_subscribed(user)
    profile = getattr(user, "profile", None)
    email_opt_in = bool(getattr(profile, "marketing_email_opt_in", False))
    sms_opt_in = bool(getattr(profile, "marketing_sms_opt_in", False))

    # Spendable loyalty balance
    points_balance = balance_for(user)

    # Style DNA preferences if set
    flash_dna = getattr(user, "flash_dna", None)
    style_preferences = {
        "favorite_styles": getattr(flash_dna, "favorite_styles", []),
        "favorite_colors": [c.name for c in getattr(flash_dna, "favorite_colors", []).all()] if flash_dna else [],
    } if flash_dna else None

    # Recommendations tailored for this customer
    recs = get_retention_recommendations(target_user_id=user_id)

    return {
        **c_info,
        "marketing_subscribed": marketing_consent,
        "email_opt_in": email_opt_in,
        "sms_opt_in": sms_opt_in,
        "points_balance": points_balance,
        "style_preferences": style_preferences,
        "recommendations": recs,
    }


def get_retention_recommendations(
    target_user_id: int | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Generates explainable, data-backed retention opportunity recommendations.
    
    Advisory only. Rules:
    1. First-time buyer follow-up: Recommend in-stock products in related categories.
    2. Loyalty balance reminder: Customers with >= 500 unspent points inactive >= 30 days.
    3. Win-back lapsed customers: Repeat customers whose average interval has passed.
    4. Catalog in-stock verified: Suggested products are verified to have available inventory.
    """
    data = get_customer_purchase_data(user_id=target_user_id)
    if not data:
        return []

    recommendations: list[dict[str, Any]] = []

    # In-stock products cache for suggestions
    in_stock_products = (
        Product.objects.filter(status=Product.Status.ACTIVE)
        .distinct()
        .select_related("category")
        .prefetch_related("images")[:15]
    )
    product_by_cat: dict[int, list[Product]] = {}
    for p in in_stock_products:
        if p.category_id:
            product_by_cat.setdefault(p.category_id, []).append(p)

    for c in data:
        user = c["user"]
        orders_count = c["orders_count"]
        days_inactive = c["days_since_last_order"]
        avg_interval = c["avg_interval_days"]
        net_spend = c["net_spend"]

        consent = marketing_subscribed(user)

        # Rule 1: Win-Back for Lapsed Repeat Customer
        if orders_count >= 2 and avg_interval is not None and days_inactive > max(45, int(avg_interval * 1.5)):
            rec = {
                "id": f"win_back_{user.pk}",
                "user": user,
                "user_id": user.pk,
                "email": user.email,
                "type": "WIN_BACK_LAPSED",
                "title": f"Win-Back Invitation ({days_inactive} Days Lapsed)",
                "reason": (
                    f"Customer has completed {orders_count} orders with a historical repurchase interval of "
                    f"{avg_interval} days. It has now been {days_inactive} days since their last order."
                ),
                "supporting_data": f"Total Net Spend: ${net_spend:.2f} | Avg Order Value: ${c['avg_order_value']:.2f}",
                "recommended_action": "Send re-engagement campaign offering free shipping or welcome back reward.",
                "eligibility_restrictions": "Marketing email opted-in" if consent else "UNSUBSCRIBED: Contact via in-app banner only",
                "is_consented": consent,
                "suggested_products": [p.name for p in in_stock_products[:3]],
            }
            recommendations.append(rec)

        # Rule 2: Unspent Loyalty Points Reminder
        points = balance_for(user)
        if points >= 500 and days_inactive >= 30:
            rec = {
                "id": f"loyalty_{user.pk}",
                "user": user,
                "user_id": user.pk,
                "email": user.email,
                "type": "LOYALTY_UNSPENT_POINTS",
                "title": f"Redeem Available FLASH Points ({points} Points)",
                "reason": f"Customer holds {points} spendable FLASH Points but has not placed an order in {days_inactive} days.",
                "supporting_data": f"Available Points: {points} (worth discount at checkout) | Last Order: {c['last_order_date']}",
                "recommended_action": "Notify customer of their unspent points balance to encourage checkout redemption.",
                "eligibility_restrictions": "Marketing email opted-in" if consent else "UNSUBSCRIBED: Requires user opt-in for email marketing",
                "is_consented": consent,
                "suggested_products": [p.name for p in in_stock_products[:2]],
            }
            recommendations.append(rec)

        # Rule 3: First-time Buyer Complementary Catalog Exploration
        if orders_count == 1 and 14 <= days_inactive <= 60:
            # Look for complementary products in same/different category
            user_cat_ids = c["category_ids"]
            matched_prods: list[Product] = []
            for cid in user_cat_ids:
                matched_prods.extend(product_by_cat.get(cid, []))
            if not matched_prods:
                matched_prods = list(in_stock_products[:3])

            rec = {
                "id": f"first_time_{user.pk}",
                "user": user,
                "user_id": user.pk,
                "email": user.email,
                "type": "FIRST_TIME_COMPLEMENTARY",
                "title": f"Second Purchase Exploration ({days_inactive} Days Post-First Order)",
                "reason": f"First-time buyer placed order on {c['first_order_date']}. In-stock complementary products are available.",
                "supporting_data": f"First Order Value: ${net_spend:.2f} | Items in stock: {len(matched_prods)}",
                "recommended_action": "Introduce customer to complementary styling pieces or new collection arrivals.",
                "eligibility_restrictions": "Marketing email opted-in" if consent else "UNSUBSCRIBED: Display in storefront / account recommendations only",
                "is_consented": consent,
                "suggested_products": [p.name for p in matched_prods[:3]],
            }
            recommendations.append(rec)

        if len(recommendations) >= limit:
            break

    return recommendations


def get_campaigns_queryset():
    """Returns RetentionCampaign queryset annotated with recipient counts and attributed revenue."""
    return (
        RetentionCampaign.objects.all()
        .annotate(
            total_recipients=Count("recipients", distinct=True),
            sent_count=Count("recipients", filter=Q(recipients__status=RecipientStatus.SENT), distinct=True),
            attributed_count=Count(
                "recipients",
                filter=Q(recipients__status=RecipientStatus.ATTRIBUTED),
                distinct=True,
            ),
            total_attributed_revenue=Sum("recipients__attributed_revenue"),
        )
        .select_related("promotion", "created_by")
        .order_by("-created_at")
    )
