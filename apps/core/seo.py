"""Site-wide structured data (Phase 20).

One rule: every value in these payloads comes from the configuration row the shop actually
holds -- the site name, the tagline, the support address -- never a hardcoded brand. The
home page emits ``Organization`` (who runs this site) and ``WebSite`` (with a real
``SearchAction`` pointing at ``/search/``, the route the search box submits to), which is
what lets a search engine attribute the site and offer its search box in results.
"""

from __future__ import annotations

from django.urls import reverse

from apps.core.context_processors import get_site_configuration


def _absolute(request, path: str) -> str:
    return request.build_absolute_uri(path) if request is not None else path


def organization_schema(request) -> dict:
    """schema.org ``Organization`` from the live site configuration."""
    site = get_site_configuration()
    payload: dict = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": site.site_name,
        "url": _absolute(request, "/"),
    }
    if site.tagline:
        payload["description"] = site.tagline
    if site.support_email:
        payload["email"] = site.support_email
    return payload


def website_schema(request) -> dict:
    """schema.org ``WebSite`` with the storefront's real search endpoint."""
    site = get_site_configuration()
    # The template braces must survive literally: running the query string through
    # ``build_absolute_uri`` would percent-encode them, and a SearchAction target with
    # ``%7Bsearch_term_string%7D`` is not a template any crawler will fill in.
    search_url = f"{_absolute(request, reverse('catalog:product-search'))}?q={{search_term_string}}"
    return {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": site.site_name,
        "url": _absolute(request, "/"),
        "potentialAction": {
            "@type": "SearchAction",
            "target": {"@type": "EntryPoint", "urlTemplate": search_url},
            "query-input": "required name=search_term_string",
        },
    }
