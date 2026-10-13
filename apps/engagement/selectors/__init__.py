from apps.engagement.selectors.retention import (
    get_campaigns_queryset,
    get_cohort_retention_matrix,
    get_customer_purchase_data,
    get_customer_retention_profile,
    get_customer_segments_summary,
    get_retention_overview_metrics,
    get_retention_recommendations,
)

# Aliases for backward/convenient imports
calculate_cohort_retention_matrix = get_cohort_retention_matrix
calculate_customer_retention_metrics = get_retention_overview_metrics
generate_retention_recommendations = get_retention_recommendations

__all__ = [
    "calculate_cohort_retention_matrix",
    "calculate_customer_retention_metrics",
    "generate_retention_recommendations",
    "get_campaigns_queryset",
    "get_cohort_retention_matrix",
    "get_customer_purchase_data",
    "get_customer_retention_profile",
    "get_customer_segments_summary",
    "get_retention_overview_metrics",
    "get_retention_recommendations",
]
