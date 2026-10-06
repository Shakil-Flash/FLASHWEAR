"""Shared SSRF-safe image and webpage fetcher for FLASHWEAR.

Provides safe downloading, URL inspection, and image extraction for the
Admin/Back Office and catalogue API.

Security guarantees:
- SSRF prevention: blocks loopback, RFC 1918 private IPs, carrier-grade NAT,
  link-local / cloud metadata (AWS/GCP/Azure/OpenStack 169.254.169.254, fd00:ec2::254),
  multicast, and dangerous URL schemes (only http and https permitted).
- Connection pinning: connects directly to the pre-validated IP address,
  preventing DNS rebinding / TOCTOU attacks while preserving TLS SNI.
- Strict timeout enforcement: connect and read timeouts.
- Safe redirect handling: redirects are followed manually and every hop is
  re-validated against the SSRF rules. Max redirect count is strictly bounded.
- Stream size enforcement: Content-Length check plus streaming byte limit to
  prevent memory exhaustion and oversized downloads.
- Content verification: checks content-type and verifies actual image bytes
  via Pillow without trusting file extension alone.
"""

from __future__ import annotations

import http.client
import ipaddress
import posixpath
import re
import socket
import ssl
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _
from PIL import Image

from apps.catalog.validators import validate_catalog_image

# --------------------------------------------------------------------------------------
# Configuration & Constants
# --------------------------------------------------------------------------------------

DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 7.0
MAX_REDIRECTS = 3
MAX_HTML_BYTES = 1024 * 1024  # 1 MB ceiling for HTML pages

# Hostnames explicitly barred even before DNS resolution
DISALLOWED_HOSTNAMES = {
    "localhost",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}

DISALLOWED_HOSTNAME_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".corp",
)

# Cloud metadata IP addresses
CLOUD_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
}

# Carrier-grade NAT network (RFC 6598)
CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")


# --------------------------------------------------------------------------------------
# Data Structures
# --------------------------------------------------------------------------------------


@dataclass
class FetchResult:
    """Raw response from an SSRF-safe HTTP request."""

    content: bytes
    content_type: str
    final_url: str
    status_code: int
    headers: dict[str, str]


@dataclass
class WebpageImageCandidate:
    """Candidate image extracted from a webpage."""

    url: str
    alt: str = ""
    source: str = "img"  # "og:image", "twitter:image", "meta", "img"


@dataclass
class WebpageExtractionResult:
    """Result of parsing candidate images from an HTML webpage."""

    title: str
    author: str
    candidates: list[WebpageImageCandidate]


@dataclass
class DownloadedImage:
    """Successfully downloaded, validated image wrapped as a ContentFile."""

    file: ContentFile
    source_url: str
    format: str
    width: int
    height: int
    size: int
    suggested_photographer: str = ""
    suggested_attribution: str = ""
    suggested_license: str = ""


# --------------------------------------------------------------------------------------
# SSRF Validation
# --------------------------------------------------------------------------------------


def is_safe_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ``ip`` is a publicly routable, safe destination IP."""
    # Unpack IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped

    if ip.is_loopback:
        return False
    if ip.is_private:
        return False
    if ip.is_link_local:
        return False
    if ip.is_multicast:
        return False
    if ip.is_reserved:
        return False
    if ip.is_unspecified:
        return False

    if isinstance(ip, ipaddress.IPv4Address):
        if ip in CARRIER_GRADE_NAT:
            return False
        if ip in CLOUD_METADATA_IPS:
            return False

    return True


def validate_url_for_ssrf(url: str) -> tuple[str, str, int, str]:
    """Validate a URL against SSRF attacks and resolve its destination IP.

    Returns:
        tuple of (scheme, hostname, port, validated_ip_string)

    Raises:
        ValidationError if the URL scheme, host, or resolved IP is forbidden.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValidationError(_("A valid URL is required."), code="empty_url")

    parsed = urlsplit(cleaned)

    # 1. Scheme check: only http and https
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValidationError(
            _("Unsupported protocol '%(scheme)s'. Only HTTP and HTTPS are allowed."),
            code="bad_scheme",
            params={"scheme": parsed.scheme or "unknown"},
        )

    # 2. Host check
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValidationError(_("URL is missing a valid hostname."), code="missing_host")

    # 3. Userinfo check
    if parsed.username or parsed.password:
        raise ValidationError(
            _("URLs containing embedded credentials are not allowed."),
            code="disallowed_userinfo",
        )

    # 4. Hostname blocklist
    if hostname in DISALLOWED_HOSTNAMES or any(
        hostname.endswith(suffix) for suffix in DISALLOWED_HOSTNAME_SUFFIXES
    ):
        raise ValidationError(
            _("Access to local or internal network hostnames is prohibited."),
            code="forbidden_host",
        )

    port = parsed.port or (443 if scheme == "https" else 80)
    if port < 1 or port > 65535:
        raise ValidationError(_("Invalid port number specified."), code="invalid_port")

    # 5. Resolve DNS and check all resolved IP addresses
    try:
        addrinfo = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValidationError(
            _("Could not resolve domain '%(host)s'."),
            code="dns_resolution_failed",
            params={"host": hostname},
        ) from error

    if not addrinfo:
        raise ValidationError(
            _("No IP addresses found for host '%(host)s'."),
            code="no_ip_found",
            params={"host": hostname},
        )

    validated_ips: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as error:
            raise ValidationError(
                _("Invalid IP address resolved for host."),
                code="invalid_ip",
            ) from error

        if not is_safe_ip(ip_obj):
            raise ValidationError(
                _("Access to internal, private, or local network addresses is prohibited."),
                code="ssrf_blocked",
            )
        validated_ips.append(ip_str)

    # Pick the first validated IP
    chosen_ip = validated_ips[0]
    return scheme, hostname, port, chosen_ip


# --------------------------------------------------------------------------------------
# SSRF-Safe HTTP Connections
# --------------------------------------------------------------------------------------


class _SSRFSafeHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects directly to a pre-validated IP address."""

    def __init__(
        self,
        host: str,
        port: int,
        actual_ip: str,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ):
        self._actual_ip = actual_ip
        super().__init__(host, port=port, timeout=timeout)

    def connect(self):
        self.sock = socket.create_connection((self._actual_ip, self.port), self.timeout)


class _SSRFSafeHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-validated IP while validating TLS for the host."""

    def __init__(
        self,
        host: str,
        port: int,
        actual_ip: str,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
        context: ssl.SSLContext | None = None,
    ):
        self._actual_ip = actual_ip
        ctx = context or ssl.create_default_context()
        super().__init__(host, port=port, timeout=timeout, context=ctx)

    def connect(self):
        self.sock = socket.create_connection((self._actual_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_url_content(
    url: str,
    *,
    max_bytes: int,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    read_timeout: float = DEFAULT_READ_TIMEOUT,
    max_redirects: int = MAX_REDIRECTS,
    user_agent: str = "FLASHWEAR-AssetFetcher/1.0",
) -> FetchResult:
    """Fetch resource bytes using an SSRF-safe connection and manual redirect handling."""
    current_url = url
    redirect_count = 0

    while True:
        scheme, hostname, port, safe_ip = validate_url_for_ssrf(current_url)
        parsed = urlsplit(current_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        conn: http.client.HTTPConnection | http.client.HTTPSConnection
        if scheme == "https":
            conn = _SSRFSafeHTTPSConnection(
                hostname, port=port, actual_ip=safe_ip, timeout=connect_timeout
            )
        else:
            conn = _SSRFSafeHTTPConnection(
                hostname, port=port, actual_ip=safe_ip, timeout=connect_timeout
            )

        try:
            conn.connect()
            if conn.sock:
                conn.sock.settimeout(read_timeout)

            headers = {
                "User-Agent": user_agent,
                "Accept": "image/webp,image/png,image/jpeg,image/*;q=0.9,text/html;q=0.8,*/*;q=0.5",
                "Accept-Encoding": "identity",  # stream uncompressed bytes directly
                "Host": hostname if port in (80, 443) else f"{hostname}:{port}",
            }
            conn.request("GET", path, headers=headers)
            resp = conn.getresponse()

            # Handle redirects manually
            if resp.status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise ValidationError(_("Redirect response missing Location header."))
                redirect_count += 1
                if redirect_count > max_redirects:
                    raise ValidationError(
                        _("Too many redirects (exceeded %(limit)d)."),
                        code="too_many_redirects",
                        params={"limit": max_redirects},
                    )
                new_url = urljoin(current_url, location)
                conn.close()
                current_url = new_url
                continue

            if resp.status != 200:
                raise ValidationError(
                    _("Remote server returned HTTP %(status)s."),
                    code="bad_http_status",
                    params={"status": resp.status},
                )

            # Check declared Content-Length
            declared_length = resp.getheader("Content-Length")
            if declared_length:
                try:
                    cl = int(declared_length)
                    if cl > max_bytes:
                        raise ValidationError(
                            _("Resource size %(size).1f MB exceeds the limit of %(limit)d MB."),
                            code="too_large",
                            params={
                                "size": cl / (1024 * 1024),
                                "limit": max_bytes // (1024 * 1024),
                            },
                        )
                except ValueError:
                    pass

            # Stream chunks with byte counter
            chunks: list[bytes] = []
            total_bytes = 0
            chunk_size = 64 * 1024
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    raise ValidationError(
                        _("Resource exceeded the maximum size limit of %(limit)d MB."),
                        code="too_large",
                        params={"limit": max_bytes // (1024 * 1024)},
                    )
                chunks.append(chunk)

            content = b"".join(chunks)
            content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}

            return FetchResult(
                content=content,
                content_type=content_type,
                final_url=current_url,
                status_code=resp.status,
                headers=resp_headers,
            )

        except TimeoutError as error:
            raise ValidationError(
                _("Connection to server timed out."),
                code="timeout",
            ) from error
        except ssl.SSLError as error:
            raise ValidationError(
                _("SSL certificate verification failed."),
                code="ssl_error",
            ) from error
        except (http.client.HTTPException, OSError) as error:
            raise ValidationError(
                _("Network error communicating with server: %(error)s"),
                code="network_error",
                params={"error": str(error)},
            ) from error
        finally:
            conn.close()


# --------------------------------------------------------------------------------------
# HTML Webpage Parser
# --------------------------------------------------------------------------------------


class _WebpageImageParser(HTMLParser):
    """Safe HTMLParser extracting OpenGraph, Twitter, and page images."""

    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.og_title: str = ""
        self.page_title: str = ""
        self.author: str = ""
        self._in_title = False
        self.og_images: list[str] = []
        self.twitter_images: list[str] = []
        self.page_images: list[tuple[str, str]] = []  # (url, alt)

    @property
    def title(self) -> str:
        return self.og_title or self.page_title

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict: dict[str, str] = {k.lower(): (v or "") for k, v in attrs if k}

        if tag.lower() == "title":
            self._in_title = True
            return

        if tag.lower() == "meta":
            prop = attr_dict.get("property", "").lower()
            name = attr_dict.get("name", "").lower()
            content = attr_dict.get("content", "").strip()

            if content:
                if prop == "og:image" or name == "og:image":
                    abs_url = urljoin(self.base_url, content)
                    if abs_url not in self.og_images:
                        self.og_images.append(abs_url)
                elif name == "twitter:image" or prop == "twitter:image":
                    abs_url = urljoin(self.base_url, content)
                    if abs_url not in self.twitter_images:
                        self.twitter_images.append(abs_url)
                elif prop in ("og:title",) and not self.og_title:
                    self.og_title = content
                elif name in ("author", "creator") or prop in ("article:author",):
                    if not self.author:
                        self.author = content

        elif tag.lower() == "img":
            # Extract src, data-src, or data-original
            src = (
                attr_dict.get("src")
                or attr_dict.get("data-src")
                or attr_dict.get("data-original")
                or ""
            ).strip()
            alt = attr_dict.get("alt", "").strip()

            if src and not src.startswith("data:"):
                # Filter out SVGs, tracking pixels, tiny icons
                lower_src = src.lower()
                if not lower_src.endswith(".svg") and not any(
                    token in lower_src
                    for token in ("tracking", "spacer", "pixel", "1x1", "favicon")
                ):
                    abs_url = urljoin(self.base_url, src)
                    self.page_images.append((abs_url, alt))

    def handle_endtag(self, tag: str):
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str):
        if self._in_title and not self.page_title:
            self.page_title = data.strip()


def extract_images_from_webpage(html_bytes: bytes, base_url: str) -> WebpageExtractionResult:
    """Parse HTML and return ranked image candidates (og:image, twitter:image, img tags)."""
    try:
        html_text = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html_text = html_bytes.decode("latin1", errors="replace")

    parser = _WebpageImageParser(base_url)
    try:
        parser.feed(html_text)
    except Exception:
        pass

    candidates: list[WebpageImageCandidate] = []
    seen_urls: set[str] = set()

    # 1. Open Graph images (top priority)
    for u in parser.og_images:
        if u not in seen_urls:
            candidates.append(WebpageImageCandidate(url=u, alt=parser.title, source="og:image"))
            seen_urls.add(u)

    # 2. Twitter Card images
    for u in parser.twitter_images:
        if u not in seen_urls:
            candidates.append(
                WebpageImageCandidate(url=u, alt=parser.title, source="twitter:image")
            )
            seen_urls.add(u)

    # 3. High-quality page images (limit to first 12 candidates)
    for u, alt in parser.page_images[:12]:
        if u not in seen_urls:
            candidates.append(WebpageImageCandidate(url=u, alt=alt or parser.title, source="page"))
            seen_urls.add(u)

    return WebpageExtractionResult(
        title=parser.title,
        author=parser.author,
        candidates=candidates,
    )


# --------------------------------------------------------------------------------------
# Filename & Format Utilities
# --------------------------------------------------------------------------------------


def _generate_safe_filename(url: str, detected_format: str) -> str:
    """Create a sanitized, collision-safe filename with the correct extension."""
    parsed = urlsplit(url)
    raw_name = posixpath.basename(unquote(parsed.path))
    stem = raw_name.rsplit(".", 1)[0] if "." in raw_name else raw_name

    # Sanitize stem
    safe_stem = re.sub(r"[^a-zA-Z0-9_\-]", "", stem).strip()
    if not safe_stem:
        safe_stem = "imported_image"
    safe_stem = safe_stem[:60]

    # Map format to standard extension
    ext_map = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    ext = ext_map.get(detected_format.upper(), ".jpg")
    return f"{safe_stem}{ext}"


# --------------------------------------------------------------------------------------
# High-Level Services
# --------------------------------------------------------------------------------------


def inspect_image_url(url: str) -> dict[str, Any]:
    """Inspect a URL to determine whether it is a direct image or a webpage.

    Returns a dict with metadata:
    - For images: ``{"type": "image", "url": ..., "content_type": ..., "size": ..., ...}``
    - For webpages: ``{"type": "webpage", "title": ..., "author": ..., "candidates": [...]}``
    """
    max_bytes = max(settings.CATALOG_IMAGE_MAX_BYTES, MAX_HTML_BYTES)
    res = fetch_url_content(url, max_bytes=max_bytes)

    # Check if direct image
    if res.content_type.startswith("image/"):
        try:
            with Image.open(BytesIO(res.content)) as img:
                img.verify()
            with Image.open(BytesIO(res.content)) as img:
                fmt = (img.format or "").upper()
                w, h = img.size
            return {
                "type": "image",
                "url": res.final_url,
                "content_type": res.content_type,
                "format": fmt,
                "width": w,
                "height": h,
                "size": len(res.content),
                "filename": _generate_safe_filename(res.final_url, fmt),
            }
        except Exception as err:
            raise ValidationError(
                _("The URL points to an unreadable or corrupted image."),
                code="unreadable_image",
            ) from err

    # Check if webpage
    is_html = (
        "html" in res.content_type
        or res.content.startswith(b"<!DOCTYPE")
        or res.content.startswith(b"<!doctype")
        or b"<html" in res.content[:500].lower()
    )

    if is_html:
        extracted = extract_images_from_webpage(res.content, res.final_url)
        if not extracted.candidates:
            return {
                "type": "webpage",
                "url": res.final_url,
                "title": extracted.title,
                "author": extracted.author,
                "candidates": [],
                "error": (
                    "This URL is a webpage, not a directly accessible image. "
                    "Please provide a direct image URL."
                ),
            }
        return {
            "type": "webpage",
            "url": res.final_url,
            "title": extracted.title,
            "author": extracted.author,
            "candidates": [
                {"url": c.url, "alt": c.alt, "source": c.source} for c in extracted.candidates
            ],
            "count": len(extracted.candidates),
        }

    # Neither image nor recognizable webpage
    raise ValidationError(
        _("URL returned unsupported content type '%(type)s'."),
        code="bad_content_type",
        params={"type": res.content_type or "unknown"},
    )


def fetch_and_validate_image_file(
    url: str,
    *,
    candidate_url: str | None = None,
    max_bytes: int | None = None,
) -> DownloadedImage:
    """Fetch an image from ``url`` (or ``candidate_url`` if ``url`` is a webpage).

    Validates SSRF, downloads within size limits, checks magic bytes with Pillow,
    and returns a ``DownloadedImage`` ready to save into a Django ``ImageField``.

    Raises:
        ValidationError with clear, user-friendly messages for all failure modes.
    """
    limit_bytes = max_bytes or settings.CATALOG_IMAGE_MAX_BYTES

    # If an explicit candidate URL was chosen from a webpage
    target_url = candidate_url or url

    # Fetch target
    res = fetch_url_content(target_url, max_bytes=limit_bytes)

    # If target is HTML (meaning user passed a webpage directly into url):
    is_html = (
        "html" in res.content_type
        or res.content.startswith(b"<!DOCTYPE")
        or res.content.startswith(b"<!doctype")
        or b"<html" in res.content[:500].lower()
    )

    suggested_author = ""
    suggested_title = ""

    if is_html:
        extracted = extract_images_from_webpage(res.content, res.final_url)
        suggested_author = extracted.author
        suggested_title = extracted.title

        if not extracted.candidates:
            raise ValidationError(
                _(
                    "This URL is a webpage, not a directly accessible image. "
                    "Please provide a direct image URL."
                ),
                code="webpage_no_image",
            )

        # Download the top candidate image
        top_candidate = extracted.candidates[0].url
        res = fetch_url_content(top_candidate, max_bytes=limit_bytes)
        if not res.content_type.startswith("image/"):
            raise ValidationError(
                _(
                    "This URL is a webpage, not a directly accessible image. "
                    "Please provide a direct image URL."
                ),
                code="webpage_no_image",
            )

    # Validate image bytes with Pillow
    temp_file = ContentFile(res.content)
    temp_file.name = "download.tmp"

    try:
        with Image.open(BytesIO(res.content)) as img:
            img.verify()
        with Image.open(BytesIO(res.content)) as img:
            detected_format = (img.format or "").upper()
            w, h = img.size
    except Exception as error:
        raise ValidationError(
            _("Downloaded file is not a readable image."),
            code="unreadable",
        ) from error

    allowed_formats = set(settings.CATALOG_IMAGE_ALLOWED_FORMATS)
    if detected_format not in allowed_formats:
        raise ValidationError(
            _("Unsupported image format (%(format)s). Allowed formats: %(allowed)s."),
            code="bad_format",
            params={"format": detected_format, "allowed": ", ".join(sorted(allowed_formats))},
        )

    # Check dimensions
    max_pixels = settings.CATALOG_IMAGE_MAX_PIXELS
    if w > max_pixels or h > max_pixels:
        raise ValidationError(
            _("Images must be at most %(limit)d pixels on each side."),
            code="too_large_dimensions",
            params={"limit": max_pixels},
        )

    # Generate final safe filename
    filename = _generate_safe_filename(res.final_url, detected_format)
    content_file = ContentFile(res.content, name=filename)

    # Run the model-level validate_catalog_image for 100% parity with file uploads
    validate_catalog_image(content_file)

    return DownloadedImage(
        file=content_file,
        source_url=url,  # preserve original input URL as the source
        format=detected_format,
        width=w,
        height=h,
        size=len(res.content),
        suggested_photographer=suggested_author,
        suggested_attribution=suggested_title,
    )
