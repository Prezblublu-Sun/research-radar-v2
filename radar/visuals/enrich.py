"""Discover reusable paper figures without touching ranking or search data.

Ported from v1 ``scripts/enrich_visuals.py``. Reads the merged corpus view,
resolves licence-safe public figures (PMC, arXiv HTML, MDPI) and returns the
records that changed; ``radar.store.visuals`` appends them as one new file in
the visuals stream. It transiently verifies bounded image responses but never
persists image or PDF bytes.

Only figures whose article metadata carries an explicit CC0, CC BY or
CC BY-SA licence are exposed; captions that signal third-party rights are
rejected even when the article licence is permissive.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Iterable
import urllib.error
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse
import urllib.request
import xml.etree.ElementTree as ET

from radar.core import identity as _identity
from radar.visuals import policy as visual_policy



SCHEMA_VERSION = "v1"
SELECTOR_VERSION = 9
# Selector milestones remain explicit because a provider addition must not
# make unrelated cached negatives (or already-safe available records) spend
# network quota again.
ARXIV_SVG_SELECTOR_VERSION = 8
MDPI_SELECTOR_VERSION = 9
# v8 adds an SVG-only fallback and deliberately leaves v7 raster selection
# unchanged.  Existing v7 available records therefore remain current and do
# not spend network quota merely because the fallback version advanced.
MIN_CURRENT_AVAILABLE_SELECTOR_VERSION = 7
DEFAULT_LIMIT = 20
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_MIN_DELAY_SECONDS = 0.5
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_SVG_BYTES = 2 * 1024 * 1024
MAX_IMAGE_CANDIDATES = 6
MIN_CARD_IMAGE_PIXELS = 4096
MAX_IMAGE_DIMENSION = 100_000
MAX_SVG_ELEMENTS = 20_000
MAX_SVG_ATTRIBUTES = 100_000
MAX_SVG_DEPTH = 64
MAX_SVG_EMBEDDED_IMAGES = 32
MAX_SVG_EMBEDDED_RASTER_PIXELS = 100_000_000
IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
SVG_MEDIA_TYPE = "image/svg+xml"
ALLOWED_LICENSES = {"CC0", "CC BY", "CC BY-SA"}

ID_CONVERTER_URL = (
    "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"
)
PMC_BUCKET_HOST = "pmc-oa-opendata.s3.amazonaws.com"
PMC_BUCKET_URL = f"https://{PMC_BUCKET_HOST}/"
ARXIV_OAI_URL = "https://oaipmh.arxiv.org/oai"
# ``export.arxiv.org/oai2`` is the former public endpoint and currently
# redirects to the dedicated OAI-PMH host.  Keep both official hosts in the
# redirect boundary so old callers remain compatible, while redirects to any
# other host still fail closed in ``HttpClient``.
ARXIV_OAI_HOSTS = {"oaipmh.arxiv.org", "export.arxiv.org"}
CROSSREF_HOST = "api.crossref.org"
CROSSREF_WORKS_URL = f"https://{CROSSREF_HOST}/works/"
MDPI_SOURCE_HOST = "www.mdpi.com"
MDPI_ASSET_HOST = "mdpi-res.com"
MDPI_ASSET_ROOT = f"https://{MDPI_ASSET_HOST}/d_attachment"
# DOI journal token -> MDPI delivery slug and electronic ISSN.  This narrow
# table is intentional: a new journal must be audited before its assets can
# cross the public-card boundary.
MDPI_JOURNALS = {
    "app": ("applsci", "2076-3417"),
    "buildings": ("buildings", "2075-5309"),
    "coatings": ("coatings", "2079-6412"),
    "designs": ("designs", "2411-9660"),
    "jmmp": ("jmmp", "2504-4494"),
    "met": ("metals", "2075-4701"),
    "psf": ("psf", "2673-9984"),
}
MDPI_XML_MEDIA_TYPES = {"application/xml", "text/xml"}
MDPI_RASTER_MEDIA_TYPES = {
    ".jpg": {"image/jpeg"},
    ".png": {"image/png"},
}

DECORATIVE_IMAGE_TOKENS = {
    "avatar", "avatars", "banner", "banners", "cover", "covers",
    "favicon", "headshot", "headshots", "icon", "icons", "logo", "logos",
    "portrait", "portraits",
}
AUXILIARY_IMAGE_TOKENS = {
    "colorbar", "colorbars", "colourbar", "colourbars",
    "key", "keys", "legend", "legends",
}


class FetchError(RuntimeError):
    """A bounded remote-fetch failure suitable for negative caching."""


class SvgValidationError(FetchError):
    """A deterministic SVG policy rejection which may try another figure."""


class MdpiImageValidationError(FetchError):
    """A deterministic MDPI raster rejection which may try another asset."""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_z(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def parse_timestamp(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_license(value: object) -> str:
    """Return a compact licence label, preserving fail-closed semantics."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    lower = raw.lower()
    if "publicdomain/zero" in lower or re.search(r"\bcc[ -]?0\b", lower):
        return "CC0"
    if "/licenses/by-sa/" in lower:
        return "CC BY-SA"
    if "/licenses/by/" in lower:
        return "CC BY"
    normalized = re.sub(r"[_-]+", " ", raw.upper())
    normalized = re.sub(r"\b(?:V?\d+(?:\.\d+)*)\b", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    tokens = set(normalized.split())
    if "CC" not in tokens and "CREATIVE COMMONS" not in normalized:
        return raw[:120]
    if "NC" in tokens and "ND" in tokens:
        return "CC BY-NC-ND"
    if "NC" in tokens and "SA" in tokens:
        return "CC BY-NC-SA"
    if "NC" in tokens:
        return "CC BY-NC"
    if "ND" in tokens:
        return "CC BY-ND"
    if "BY" in tokens and "SA" in tokens:
        return "CC BY-SA"
    if "BY" in tokens:
        return "CC BY"
    return raw[:120]


def _creative_commons_license_url(
        value: object, *, allow_http: bool = False) -> str:
    """Return an allowlisted Creative Commons licence label from an exact URL."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    allowed_schemes = {"https", "http"} if allow_http else {"https"}
    if (parsed.scheme not in allowed_schemes or parsed.netloc.lower() !=
            "creativecommons.org" or parsed.username or parsed.password or
            parsed.port is not None or parsed.query or parsed.fragment or
            parsed.params):
        return ""
    match = re.fullmatch(
        r"/licenses/(by|by-sa)/(\d+(?:\.\d+)?)/", parsed.path.lower(),
    )
    if match:
        return "CC BY-SA" if match.group(1) == "by-sa" else "CC BY"
    if re.fullmatch(
            r"/publicdomain/zero/(\d+(?:\.\d+)?)/",
            parsed.path.lower()):
        return "CC0"
    return ""


def _text_http_urls(value: object) -> list[str]:
    """Extract complete HTTP(S) URLs from prose, trimming sentence punctuation."""
    return [
        match.rstrip(".,;:)]}")
        for match in re.findall(
            r"https?://[^\s<>\"']+", str(value or ""), flags=re.IGNORECASE,
        )
    ]


def _jats_license_text_has_restrictions(value: object) -> bool:
    """Detect explicit non-commercial or no-derivatives licence qualifiers."""
    text = " ".join(str(value or "").lower().split())
    return bool(re.search(
        r"\b(?:nc|nd)\b|"
        r"\bnon[\s-]*commercial\b|"
        r"\bno[\s-]*derivatives?\b|"
        r"\bno[\s-]*derivs?\b|"
        r"\ball rights reserved\b|"
        r"\bnot (?:covered|included)\b|"
        r"\bexcluded from\b|"
        r"\bthird[\s-]*party\b",
        text,
    ))


def _crossref_license_started(record: dict, as_of: dt.datetime) -> bool:
    """Validate an optional Crossref licence start and require it to be live."""
    if "start" not in record:
        return True
    start = record.get("start")
    if not isinstance(start, dict) or not start or not set(start) <= {
            "date-parts", "date-time", "timestamp"}:
        return False

    precise: list[dt.datetime] = []
    parts: list[int] | None = None
    if "date-time" in start:
        if not isinstance(start["date-time"], str):
            return False
        parsed = parse_timestamp(start["date-time"])
        if parsed is None:
            return False
        precise.append(parsed)
    if "timestamp" in start:
        timestamp = start["timestamp"]
        if (not isinstance(timestamp, (int, float)) or
                isinstance(timestamp, bool) or not math.isfinite(timestamp)):
            return False
        try:
            parsed = dt.datetime.fromtimestamp(
                timestamp / 1000, tz=dt.timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            return False
        precise.append(parsed)
    if "date-parts" in start:
        raw_parts = start["date-parts"]
        if (not isinstance(raw_parts, list) or len(raw_parts) != 1 or
                not isinstance(raw_parts[0], list) or
                not 1 <= len(raw_parts[0]) <= 3 or
                any(not isinstance(value, int) or isinstance(value, bool)
                    for value in raw_parts[0])):
            return False
        parts = raw_parts[0]
        try:
            dt.datetime(
                parts[0], parts[1] if len(parts) > 1 else 1,
                parts[2] if len(parts) > 2 else 1,
                tzinfo=dt.timezone.utc,
            )
        except ValueError:
            return False
    if not precise and parts is None:
        return False
    if len(precise) > 1 and max(precise) - min(precise) > dt.timedelta(
            seconds=1):
        return False
    if parts is not None:
        for value in precise:
            actual = (value.year, value.month, value.day)
            if tuple(parts) != actual[:len(parts)]:
                return False
        effective = dt.datetime(
            parts[0], parts[1] if len(parts) > 1 else 1,
            parts[2] if len(parts) > 2 else 1,
            tzinfo=dt.timezone.utc,
        )
    else:
        effective = max(precise)
    return effective <= as_of.astimezone(dt.timezone.utc)


def _crossref_vor_license_name(metadata: dict, as_of: dt.datetime) -> str:
    """Return one consistently allowlisted, effective Crossref VOR licence."""
    licenses = metadata.get("license")
    if not isinstance(licenses, list) or not licenses:
        return ""
    names = set()
    for record in licenses:
        if not isinstance(record, dict):
            return ""
        if str(record.get("content-version") or "").strip().lower() != "vor":
            return ""
        if not _crossref_license_started(record, as_of):
            return ""
        name = _creative_commons_license_url(record.get("URL"))
        if not name:
            return ""
        names.add(name)
    return next(iter(names)) if len(names) == 1 else ""


def _mdpi_doi_context(value: object) -> dict | None:
    """Parse only an audited ``10.3390`` journal DOI family."""
    doi = str(value or "").strip().lower()
    for token, (slug, issn) in MDPI_JOURNALS.items():
        if re.fullmatch(rf"10\.3390/{re.escape(token)}\d+", doi):
            return {"doi": doi, "token": token, "slug": slug, "issn": issn}
    return None


def _mdpi_source_context(value: object, doi_context: dict) -> dict | None:
    """Validate an exact official MDPI article URL and derive its asset stem."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if (parsed.scheme != "https" or parsed.netloc.lower() != MDPI_SOURCE_HOST or
            parsed.username or parsed.password or parsed.port is not None or
            parsed.params or parsed.query or parsed.fragment or
            unquote(parsed.path) != parsed.path):
        return None
    match = re.fullmatch(
        r"/(\d{4}-\d{3}[\dXx])/(\d+)/(\d+)/(\d+)", parsed.path,
    )
    if not match or match.group(1).upper() != doi_context["issn"].upper():
        return None
    volume, issue, article = (int(value) for value in match.groups()[1:])
    if not volume or not issue or not article:
        return None
    slug = doi_context["slug"]
    stem = f"{slug}-{volume:02d}-{article:05d}"
    return {
        **doi_context,
        "source_url": raw,
        "volume": volume,
        "issue": issue,
        "article": article,
        "stem": stem,
    }


def _mdpi_xml_url(context: dict) -> str:
    slug = str(context["slug"])
    stem = str(context["stem"])
    return (
        f"{MDPI_ASSET_ROOT}/{slug}/{stem}/article_deploy/{stem}.xml"
    )


def _mdpi_asset_url(value: object, context: dict | None = None) -> str:
    """Return an exact audited MDPI browser-raster URL, or an empty string."""
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if (parsed.scheme != "https" or parsed.netloc.lower() != MDPI_ASSET_HOST or
            parsed.username or parsed.password or parsed.port is not None or
            parsed.params or parsed.query or parsed.fragment or
            unquote(parsed.path) != parsed.path or "\\" in parsed.path):
        return ""
    match = re.fullmatch(
        r"/d_attachment/([a-z][a-z0-9]*)/"
        r"([a-z][a-z0-9]*-\d{2}-\d{5})/article_deploy/html/images/"
        r"([a-z][a-z0-9]*-\d{2}-\d{5}-g\d{3})"
        r"(?:-550\.jpg|\.png)",
        parsed.path,
    )
    if not match:
        return ""
    slug, stem, graphic_stem = match.groups()
    if not stem.startswith(f"{slug}-") or not graphic_stem.startswith(
            f"{stem}-"):
        return ""
    graphic_number = graphic_stem.rsplit("-g", 1)[-1]
    if graphic_number == "000":
        return ""
    if context is not None and (
            slug != context.get("slug") or stem != context.get("stem")):
        return ""
    return raw


def _http_not_found(exc: FetchError) -> bool:
    return str(exc) in {"HTTP 404", "HTTP 410"}


def caption_has_third_party_rights(caption: str) -> bool:
    """Compatibility wrapper around the shared public-renderer policy."""
    return visual_policy.has_third_party_figure_rights(caption)


def looks_like_decorative_image(*hints: object) -> bool:
    """Reject branding and author imagery before ranking paper figures."""
    tokens = set(re.findall(
        r"[a-z0-9]+", " ".join(str(value or "") for value in hints).lower(),
    ))
    return bool(tokens & DECORATIVE_IMAGE_TOKENS) or (
        {"author", "photo"} <= tokens or {"journal", "cover"} <= tokens
    )


def looks_like_auxiliary_image(asset: object, *hints: object) -> bool:
    """Reject legend/key assets that are not meaningful standalone figures."""
    path = unquote(urlparse(str(asset or "")).path).lower()
    if re.search(r"(?:^|/)legal-mentions/", path):
        return True
    path_tokens = set(re.findall(r"[a-z0-9]+", path))
    if path_tokens & (AUXILIARY_IMAGE_TOKENS - {"key", "keys"}):
        return True
    if re.search(
            r"(?:^|[^a-z0-9])(?:only[_-]?)?"
            r"(?:cbar|colou?r[\s_-]*bars?)(?:$|[^a-z0-9])",
            path,
    ):
        return True
    stem = Path(path).stem
    if re.fullmatch(
            r"(?:(?:plot|chart|figure|color|colour)[_-]?)?"
            r"keys?(?:[_-]?\d+)?",
            stem,
    ):
        return True

    # Descriptive attributes occasionally identify an auxiliary asset even
    # when its generated filename is opaque.  Require the whole description
    # to look auxiliary so phrases such as "key experimental result" remain
    # eligible.
    pattern = re.compile(
        r"(?:(?:plot|chart|figure|color|colour) )?"
        r"(?:legend|key|colou?r bars?)s?"
    )
    for value in hints:
        description = " ".join(re.findall(
            r"[a-z0-9]+", str(value or "").lower(),
        ))
        if pattern.fullmatch(description):
            return True
    return False


def _suffix(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def _https_url(value: object, *, hosts: set[str]) -> str:
    """Validate/upgrade a URL and return an allowlisted HTTPS URL."""
    raw = str(value or "").strip()
    if raw.startswith("s3://pmc-oa-opendata/"):
        key = raw[len("s3://pmc-oa-opendata/"):].split("?", 1)[0]
        raw = PMC_BUCKET_URL + key
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in hosts:
        return ""
    if host == PMC_BUCKET_HOST and parsed.path.startswith("/deprecated/"):
        return ""
    return parsed._replace(scheme="https").geturl()


def pmc_media_url(value: object) -> str:
    url = _https_url(value, hosts={PMC_BUCKET_HOST})
    return url if url and _suffix(url) in IMAGE_SUFFIXES else ""


def identity_key(paper: dict) -> str:
    """The exact, non-normalising public identity (doi:/arxiv:)."""
    return _identity.identity_key(paper)


def _visual_lookup_identity(identity: str) -> str:
    """Return a provider lookup alias without changing a public identity.

    DOI names are case-insensitive and an arXiv ``vN`` suffix selects a
    version of the same work.  The renderer/localStorage contract deliberately
    keeps those strings exact, so this normalized value is only used inside
    enrichment to share cache entries and remote requests.
    """
    if not isinstance(identity, str):
        return ""
    if identity.startswith("doi:"):
        value = identity[len("doi:"):].strip()
        return f"doi:{value.lower()}" if value else ""
    if identity.startswith("arxiv:"):
        value = identity[len("arxiv:"):].strip()
        base = re.sub(r"v\d+$", "", value, flags=re.IGNORECASE)
        return f"arxiv:{base}" if base else ""
    return ""


def visual_lookup_key(paper: dict) -> str:
    """Return the private request/cache key for an exact public paper key."""
    return _visual_lookup_identity(identity_key(paper))


def _truncate_text(value: str, limit: int) -> str:
    """Bound display text at a word boundary outside obvious LaTeX spans."""
    if len(value) <= limit:
        return value

    math_delimiter = ""
    brace_depth = 0
    last_boundary = 0
    index = 0
    while index < limit:
        char = value[index]
        escaped = index > 0 and value[index - 1] == "\\"
        pair = value[index:index + 2]

        if not escaped and pair in {"\\(", "\\["} and not math_delimiter:
            math_delimiter = pair
            index += 2
            continue
        if pair in {"\\)", "\\]"} and math_delimiter:
            expected = "\\)" if math_delimiter == "\\(" else "\\]"
            if pair == expected:
                math_delimiter = ""
            index += 2
            continue
        if char == "$" and not escaped and not math_delimiter:
            math_delimiter = "$$" if value[index:index + 2] == "$$" else "$"
            index += len(math_delimiter)
            continue
        if char == "$" and not escaped and math_delimiter in {"$", "$$"}:
            closing = math_delimiter
            if value[index:index + len(closing)] == closing:
                math_delimiter = ""
                index += len(closing)
                continue

        if not escaped and not math_delimiter:
            if char == "{":
                brace_depth += 1
            elif char == "}" and brace_depth:
                brace_depth -= 1
            elif char.isspace() and brace_depth == 0:
                last_boundary = index
        index += 1

    # The hard boundary itself may already fall between complete words.
    if (not math_delimiter and brace_depth == 0 and limit < len(value) and
            (value[limit].isspace() or not value[limit - 1].isalnum() or
             not value[limit].isalnum())):
        last_boundary = limit
    if last_boundary:
        return value[:last_boundary].rstrip()
    # A single pathological token can exceed the whole limit.  Retaining the
    # established hard cap is safer than allowing unbounded registry content.
    return value[:limit].rstrip()


def _blank_visual(status: str, *, checked_at: str, reason: str = "",
                  license_name: str = "", provider: str = "") -> dict:
    result = {
        "status": status,
        "image_url": "",
        "caption": "",
        "source_label": "",
        "source_url": "",
        "license": license_name,
        "alt": "",
        "width": None,
        "height": None,
        "checked_at": checked_at,
        "selector_version": SELECTOR_VERSION,
    }
    if reason:
        result["reason"] = reason
    if provider:
        result["provider"] = provider
    return result


def _available_visual(*, checked_at: str, image_url: str, caption: str,
                      source_label: str, source_url: str,
                      license_name: str, alt: str, provider: str,
                      width: int | None = None,
                      height: int | None = None,
                      media_type: str = "") -> dict:
    result = {
        "status": "available",
        "image_url": image_url,
        "caption": _truncate_text(caption, 1200),
        "source_label": source_label[:160],
        "source_url": source_url,
        "license": license_name,
        "alt": _truncate_text(alt or caption or "论文插图", 500),
        "width": width,
        "height": height,
        "checked_at": checked_at,
        "provider": provider,
        "selector_version": SELECTOR_VERSION,
    }
    if media_type:
        result["media_type"] = media_type
    return result


class HttpClient:
    """Rate-limited HTTP reader with small retry and response-size bounds."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 min_delay: float = DEFAULT_MIN_DELAY_SECONDS,
                 max_attempts: int = 2,
                 opener: urllib.request.OpenerDirector | None = None):
        self.timeout = timeout
        self.min_delay = min_delay
        self.max_attempts = max(1, max_attempts)
        self.opener = opener or urllib.request.build_opener()
        self.headers = {
            "User-Agent": "ResearchRadarVisuals/1.0 (metadata enrichment)",
            "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.1",
        }
        self._last_request_at = 0.0

    def _wait(self) -> None:
        remaining = self.min_delay - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _get_bytes_response(
            self, url: str, *, params: dict | None = None,
            allowed_hosts: set[str],
            max_bytes: int = MAX_METADATA_BYTES,
    ) -> tuple[bytes, str, str]:
        """Return bounded bytes, response media type, and final URL."""
        if params:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params, doseq=True)}"
        initial = urlparse(url)
        if initial.scheme != "https" or (initial.hostname or "").lower() not in allowed_hosts:
            raise FetchError("remote URL is outside the provider allowlist")
        last_error = "request failed"
        for attempt in range(self.max_attempts):
            self._wait()
            try:
                request = urllib.request.Request(url, headers=self.headers)
                response = self.opener.open(request, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                final = urlparse(response.geturl())
                if final.scheme != "https" or (
                    final.hostname or ""
                ).lower() not in allowed_hosts:
                    response.close()
                    raise FetchError("provider redirected outside its allowlist")
                try:
                    declared = response.headers.get("Content-Length")
                    if declared and int(declared) > max_bytes:
                        raise FetchError("provider response exceeded size limit")
                    media_type = str(
                        response.headers.get("Content-Type") or ""
                    ).split(";", 1)[0].strip().lower()
                    payload = response.read(max_bytes + 1)
                finally:
                    response.close()
                if len(payload) > max_bytes:
                    raise FetchError("provider response exceeded size limit")
                return payload, media_type, final.geturl()
            except urllib.error.HTTPError as exc:
                self._last_request_at = time.monotonic()
                last_error = f"HTTP {exc.code}"
                if (exc.code in {429, 500, 502, 503, 504} and
                        attempt + 1 < self.max_attempts):
                    time.sleep(min(2.0, 0.5 * (2 ** attempt)))
                    continue
                break
            except (urllib.error.URLError, TimeoutError, OSError,
                    FetchError) as exc:
                last_error = str(exc) or type(exc).__name__
                if isinstance(exc, FetchError) or attempt + 1 >= self.max_attempts:
                    break
                time.sleep(min(2.0, 0.5 * (2 ** attempt)))
        raise FetchError(last_error)

    def get_bytes(self, url: str, *, params: dict | None = None,
                  allowed_hosts: set[str],
                  max_bytes: int = MAX_METADATA_BYTES) -> bytes:
        payload, _media_type, _final_url = self._get_bytes_response(
            url, params=params, allowed_hosts=allowed_hosts,
            max_bytes=max_bytes,
        )
        return payload

    def get_json(self, url: str, *, params: dict | None = None,
                 allowed_hosts: set[str]) -> dict:
        try:
            payload = json.loads(self.get_bytes(
                url, params=params, allowed_hosts=allowed_hosts,
            ).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError("provider returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FetchError("provider JSON root is not an object")
        return payload

    def get_crossref_work(self, doi: str) -> dict:
        """Fetch an exact Crossref work record without following path changes."""
        url = f"{CROSSREF_WORKS_URL}{quote(doi, safe='')}"
        payload, media_type, final_url = self._get_bytes_response(
            url, allowed_hosts={CROSSREF_HOST}, max_bytes=MAX_METADATA_BYTES,
        )
        if final_url != url:
            raise FetchError("Crossref redirected away from the exact work URL")
        if media_type != "application/json":
            raise FetchError("Crossref work response has an invalid media type")
        try:
            root = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FetchError("Crossref returned invalid JSON") from exc
        message = root.get("message") if isinstance(root, dict) else None
        if not isinstance(message, dict):
            raise FetchError("Crossref work response has no message object")
        return message

    def get_text(self, url: str, *, params: dict | None = None,
                 allowed_hosts: set[str]) -> str:
        return self.get_bytes(
            url, params=params, allowed_hosts=allowed_hosts,
        ).decode("utf-8", errors="replace")

    def get_mdpi_xml(self, url: str) -> str:
        """Fetch one exact official MDPI JATS object with an XML MIME type."""
        payload, media_type, final_url = self._get_bytes_response(
            url, allowed_hosts={MDPI_ASSET_HOST},
            max_bytes=MAX_METADATA_BYTES,
        )
        if final_url != url:
            raise FetchError("MDPI JATS redirected away from its exact asset URL")
        if media_type not in MDPI_XML_MEDIA_TYPES:
            raise FetchError("MDPI JATS response has an invalid media type")
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FetchError("MDPI JATS response is not UTF-8 XML") from exc

    def verify_image(self, url: str, *,
                     allowed_hosts: set[str]) -> tuple[int, int]:
        """Fetch one bounded raster and return its intrinsic pixel dimensions."""
        payload = self.get_bytes(
            url, allowed_hosts=allowed_hosts, max_bytes=MAX_IMAGE_BYTES,
        )
        return _raster_dimensions(payload)

    def verify_mdpi_image(self, url: str) -> tuple[int, int]:
        """Verify an exact MDPI raster, including URL, redirect, and MIME."""
        if not _mdpi_asset_url(url):
            raise MdpiImageValidationError(
                "MDPI image URL is outside its exact asset path"
            )
        payload, media_type, final_url = self._get_bytes_response(
            url, allowed_hosts={MDPI_ASSET_HOST}, max_bytes=MAX_IMAGE_BYTES,
        )
        if final_url != url or not _mdpi_asset_url(final_url):
            raise MdpiImageValidationError(
                "MDPI image redirected away from its exact asset URL"
            )
        if media_type not in MDPI_RASTER_MEDIA_TYPES.get(_suffix(url), set()):
            raise MdpiImageValidationError(
                "MDPI image response has an invalid media type"
            )
        try:
            return _raster_dimensions(payload)
        except FetchError as exc:
            raise MdpiImageValidationError(str(exc)) from exc

    def verify_svg(self, url: str, *,
                   allowed_hosts: set[str]) -> tuple[int, int]:
        """Fetch and strictly validate one bounded, passive arXiv SVG."""
        if allowed_hosts != {"arxiv.org"} or not _arxiv_svg_url(url):
            raise SvgValidationError("SVG URL is outside the arXiv HTML asset path")
        payload, media_type, final_url = self._get_bytes_response(
            url, allowed_hosts=allowed_hosts, max_bytes=MAX_SVG_BYTES,
        )
        if (not _arxiv_svg_url(final_url) or
                _arxiv_html_work_id(final_url) != _arxiv_html_work_id(url)):
            raise SvgValidationError(
                "SVG redirected outside its arXiv HTML work path"
            )
        if media_type != SVG_MEDIA_TYPE:
            raise SvgValidationError(
                "provider SVG response has an invalid media type"
            )
        return _svg_dimensions(payload)


def _valid_image_dimensions(width: int, height: int) -> tuple[int, int]:
    if (width <= 0 or height <= 0 or width > MAX_IMAGE_DIMENSION or
            height > MAX_IMAGE_DIMENSION):
        raise FetchError("provider image response has invalid dimensions")
    return width, height


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    if not payload.startswith(b"\xff\xd8\xff"):
        raise FetchError("provider image response has an invalid signature")
    # Start-of-frame markers which carry dimensions.  DHT/JPG/DAC and the
    # restart markers deliberately are not included.
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    while offset < len(payload):
        if payload[offset] != 0xFF:
            raise FetchError("provider image response has invalid JPEG data")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0x01, 0xD8} or 0xD0 <= marker <= 0xD7:
            continue
        if marker in {0xD9, 0xDA}:
            break
        if offset + 2 > len(payload):
            raise FetchError("provider image response has truncated JPEG data")
        segment_length = int.from_bytes(payload[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            raise FetchError("provider image response has truncated JPEG data")
        if marker in sof_markers:
            if segment_length < 8:
                raise FetchError("provider image response has invalid JPEG data")
            component_count = payload[offset + 7]
            if (component_count <= 0 or
                    segment_length != 8 + 3 * component_count):
                raise FetchError("provider image response has invalid JPEG data")
            height = int.from_bytes(payload[offset + 3:offset + 5], "big")
            width = int.from_bytes(payload[offset + 5:offset + 7], "big")
            return _valid_image_dimensions(width, height)
        offset += segment_length
    raise FetchError("provider image response has no JPEG dimensions")


def _webp_dimensions(payload: bytes) -> tuple[int, int]:
    if (len(payload) < 12 or not payload.startswith(b"RIFF") or
            payload[8:12] != b"WEBP"):
        raise FetchError("provider image response has an invalid signature")
    declared_end = 8 + int.from_bytes(payload[4:8], "little")
    if declared_end < 12 or declared_end > len(payload):
        raise FetchError("provider image response has truncated WebP data")
    offset = 12
    while offset + 8 <= declared_end:
        kind = payload[offset:offset + 4]
        size = int.from_bytes(payload[offset + 4:offset + 8], "little")
        start = offset + 8
        end = start + size
        if end > declared_end:
            raise FetchError("provider image response has truncated WebP data")
        data = payload[start:end]
        if kind == b"VP8X":
            if len(data) < 10:
                raise FetchError("provider image response has invalid WebP data")
            width = int.from_bytes(data[4:7], "little") + 1
            height = int.from_bytes(data[7:10], "little") + 1
            return _valid_image_dimensions(width, height)
        if kind == b"VP8L":
            if len(data) < 5 or data[0] != 0x2F:
                raise FetchError("provider image response has invalid WebP data")
            bits = int.from_bytes(data[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            return _valid_image_dimensions(width, height)
        if kind == b"VP8 ":
            if len(data) < 10 or data[3:6] != b"\x9d\x01\x2a":
                raise FetchError("provider image response has invalid WebP data")
            width = int.from_bytes(data[6:8], "little") & 0x3FFF
            height = int.from_bytes(data[8:10], "little") & 0x3FFF
            return _valid_image_dimensions(width, height)
        offset = end + (size & 1)
    raise FetchError("provider image response has no WebP dimensions")


def _raster_dimensions(payload: bytes) -> tuple[int, int]:
    """Read intrinsic dimensions from the four allowlisted raster formats."""
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        if (len(payload) < 33 or
                int.from_bytes(payload[8:12], "big") != 13 or
                payload[12:16] != b"IHDR"):
            raise FetchError("provider image response has invalid PNG data")
        return _valid_image_dimensions(
            int.from_bytes(payload[16:20], "big"),
            int.from_bytes(payload[20:24], "big"),
        )
    if payload.startswith((b"GIF87a", b"GIF89a")):
        if len(payload) < 13:
            raise FetchError("provider image response has truncated GIF data")
        return _valid_image_dimensions(
            int.from_bytes(payload[6:8], "little"),
            int.from_bytes(payload[8:10], "little"),
        )
    if payload.startswith(b"\xff\xd8\xff"):
        return _jpeg_dimensions(payload)
    if (len(payload) >= 12 and payload.startswith(b"RIFF") and
            payload[8:12] == b"WEBP"):
        return _webp_dimensions(payload)
    raise FetchError("provider image response has an invalid signature")


def _arxiv_svg_url(value: object) -> str:
    """Return an exact passive-asset URL under ``https://arxiv.org/html/``.

    SVG gets a narrower boundary than ordinary links because the browser will
    parse it as an image document.  User-info, ports, queries, fragments,
    encoded path components, traversal, and non-HTML arXiv endpoints all fail
    closed.  Relative references are resolved before reaching this helper.
    """
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    path = parsed.path
    asset_path = path[len("/html/"):] if path.startswith("/html/") else ""
    versioned_id = bool(re.fullmatch(
        r"(?:\d{4}\.\d{4,5}v[1-9]\d*|"
        r"[a-z][a-z0-9.-]*/\d{7}v[1-9]\d*)/.+\.svg",
        asset_path,
        flags=re.IGNORECASE,
    ))
    if (parsed.scheme != "https" or parsed.netloc.lower() != "arxiv.org" or
            parsed.query or parsed.fragment or not versioned_id or
            not path.lower().endswith(".svg") or "%" in path or
            "\\" in path or any(
                part in {"", ".", ".."} for part in asset_path.split("/")
            )):
        return ""
    return parsed.geturl()


def _arxiv_html_work_id(value: object) -> str:
    """Return the normalized work id from an arXiv HTML page or asset."""
    parsed = urlparse(str(value or "").strip())
    if (parsed.scheme != "https" or parsed.netloc.lower() != "arxiv.org" or
            not parsed.path.startswith("/html/")):
        return ""
    relative = parsed.path[len("/html/"):]
    match = re.match(
        r"(?P<id>\d{4}\.\d{4,5}(?:v[1-9]\d*)?|"
        r"[a-z][a-z0-9.-]*/\d{7}(?:v[1-9]\d*)?)(?:/|$)",
        relative,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    return re.sub(r"v\d+$", "", match.group("id"), flags=re.IGNORECASE).lower()


def _svg_length(value: object) -> float:
    """Parse one finite absolute SVG length into CSS reference pixels."""
    raw = str(value or "").strip().lower()
    match = re.fullmatch(
        r"([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?)"
        r"(px|pt|pc|in|cm|mm|q)?",
        raw,
    )
    if not match:
        return 0.0
    number = float(match.group(1))
    scale = {
        "": 1.0, "px": 1.0, "pt": 96 / 72, "pc": 16.0,
        "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4,
        "q": 96 / 101.6,
    }[match.group(2) or ""]
    pixels = number * scale
    return pixels if math.isfinite(pixels) and pixels > 0 else 0.0


def _svg_viewbox_dimensions(value: object) -> tuple[float, float]:
    raw = str(value or "").strip()
    parts = [part for part in re.split(r"[\s,]+", raw) if part]
    if len(parts) != 4:
        return 0.0, 0.0
    try:
        numbers = [float(part) for part in parts]
    except ValueError:
        return 0.0, 0.0
    if not all(math.isfinite(number) for number in numbers):
        return 0.0, 0.0
    width, height = numbers[2:]
    return (width, height) if width > 0 and height > 0 else (0.0, 0.0)


def _embedded_raster(value: str) -> tuple[int, int]:
    """Validate a base64 raster data URI without allowing nested SVG."""
    match = re.fullmatch(
        r"data:(image/(?:png|jpeg|gif|webp));base64,([A-Za-z0-9+/=\s]+)",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        raise SvgValidationError("SVG contains a non-raster data reference")
    encoded = re.sub(r"\s+", "", match.group(2))
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SvgValidationError("SVG contains invalid base64 raster data") from exc
    try:
        dimensions = _raster_dimensions(payload)
    except FetchError as exc:
        raise SvgValidationError("SVG contains invalid embedded raster data") from exc
    if dimensions[0] * dimensions[1] > MAX_SVG_EMBEDDED_RASTER_PIXELS:
        raise SvgValidationError("SVG embedded raster exceeds the pixel limit")
    declared = match.group(1).lower()
    signature_type = (
        "image/png" if payload.startswith(b"\x89PNG\r\n\x1a\n") else
        "image/gif" if payload.startswith((b"GIF87a", b"GIF89a")) else
        "image/jpeg" if payload.startswith(b"\xff\xd8\xff") else
        "image/webp"
    )
    if declared != signature_type:
        raise SvgValidationError("SVG embedded raster media type is misleading")
    return dimensions


def _svg_dimensions(payload: bytes) -> tuple[int, int]:
    """Validate passive SVG XML and return bounded intrinsic dimensions."""
    try:
        decoded = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SvgValidationError("provider SVG response is not UTF-8 XML") from exc
    if decoded.startswith("\ufeff"):
        raise SvgValidationError("provider SVG response has a byte-order mark")
    declaration = re.match(r"\s*<\?xml\s+([^?]+)\?>", decoded,
                           flags=re.IGNORECASE)
    if declaration:
        encoding = re.search(
            r"\bencoding\s*=\s*(['\"])([^'\"]+)\1",
            declaration.group(1),
            flags=re.IGNORECASE,
        )
        if encoding and encoding.group(2).lower().replace("_", "-") not in {
                "utf-8", "utf8"}:
            raise SvgValidationError("provider SVG declares a non-UTF-8 encoding")
    upper = payload.upper()
    if (b"<!DOCTYPE" in upper or b"<!ENTITY" in upper or
            b"<?XML-STYLESHEET" in upper):
        raise SvgValidationError("SVG contains a forbidden XML declaration")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SvgValidationError("provider SVG response has invalid XML") from exc
    if root.tag != "{http://www.w3.org/2000/svg}svg":
        raise SvgValidationError("provider SVG response has a non-SVG root")

    active_elements = {
        "script", "foreignobject", "iframe", "object", "embed", "audio",
        "video", "canvas", "animate", "animatecolor", "animatemotion",
        "animatetransform", "set", "discard", "handler", "style",
    }
    element_count = 0
    attribute_count = 0
    embedded_images = 0
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        element_count += 1
        attribute_count += len(node.attrib)
        if (element_count > MAX_SVG_ELEMENTS or
                attribute_count > MAX_SVG_ATTRIBUTES or
                depth > MAX_SVG_DEPTH):
            raise SvgValidationError("SVG exceeds the complexity limit")
        tag = _local_name(node.tag)
        if tag in active_elements:
            raise SvgValidationError("SVG contains active content")
        for raw_name, raw_value in node.attrib.items():
            name = _local_name(raw_name)
            value = str(raw_value or "").strip()
            lower = value.lower()
            if name.startswith("on"):
                raise SvgValidationError("SVG contains an event handler")
            if name == "base":
                raise SvgValidationError("SVG contains an XML base URL")
            if name in {"href", "src"} and value:
                if value.startswith("#") and re.fullmatch(
                        r"#[A-Za-z_][A-Za-z0-9_.:-]*", value):
                    pass
                elif name == "href" and tag == "image" and lower.startswith(
                        "data:image/"
                ):
                    embedded_images += 1
                    if embedded_images > MAX_SVG_EMBEDDED_IMAGES:
                        raise SvgValidationError(
                            "SVG contains too many embedded raster images"
                        )
                    _embedded_raster(value)
                else:
                    raise SvgValidationError("SVG contains an external reference")
            if ("javascript:" in lower or "vbscript:" in lower or
                    "@import" in lower or "expression(" in lower):
                raise SvgValidationError("SVG contains active CSS or a script URL")
            if (name not in {"href", "src"} and
                    ("\\" in value or "/*" in value or "*/" in value or
                     re.search(r"(?:https?:|data:|//)", lower))):
                raise SvgValidationError("SVG contains an obfuscated reference")
            if name == "style" and (
                    "\\" in value or "@" in value or
                    re.search(r"(?:https?:|data:|//)", lower)):
                raise SvgValidationError("SVG contains non-passive inline CSS")
            for reference in re.findall(
                    r"url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)", value,
                    flags=re.IGNORECASE):
                if not re.fullmatch(
                        r"#[A-Za-z_][A-Za-z0-9_.:-]*", reference.strip()):
                    raise SvgValidationError("SVG contains an external CSS reference")
        stack.extend((child, depth + 1) for child in node)

    raw_width = root.attrib.get("width")
    raw_height = root.attrib.get("height")
    has_width = raw_width is not None and str(raw_width).strip() != ""
    has_height = raw_height is not None and str(raw_height).strip() != ""
    if has_width != has_height:
        raise SvgValidationError("provider SVG response has invalid dimensions")
    if has_width:
        width = _svg_length(raw_width)
        height = _svg_length(raw_height)
        # An explicitly invalid intrinsic size must not silently borrow a
        # valid viewBox.  That would turn zero, negative, relative, or
        # malformed author geometry into an apparently usable card asset.
        if not width or not height:
            raise SvgValidationError("provider SVG response has invalid dimensions")
    else:
        width, height = _svg_viewbox_dimensions(root.attrib.get("viewBox"))
    if (not width or not height or width > MAX_IMAGE_DIMENSION or
            height > MAX_IMAGE_DIMENSION):
        raise SvgValidationError("provider SVG response has invalid dimensions")
    rounded_width = round(width)
    rounded_height = round(height)
    if rounded_width <= 0 or rounded_height <= 0:
        raise SvgValidationError("provider SVG response has invalid dimensions")
    return _valid_image_dimensions(rounded_width, rounded_height)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _graphic_href(fig: ET.Element) -> str:
    # A caption may contain a small inline symbol before the figure's actual
    # graphic.  Prefer JATS ``graphic`` regardless of document order; retain
    # ``inline-graphic`` only as a compatibility fallback.
    for wanted_tag in ("graphic", "inline-graphic"):
        for node in fig.iter():
            if _local_name(node.tag) != wanted_tag:
                continue
            for key, value in node.attrib.items():
                if key == "href" or key.endswith("}href"):
                    return value
    return ""


def _basename(value: str) -> str:
    return Path(unquote(urlparse(value).path)).name.lower()


def _match_media(href: str, media_urls: list[str]) -> str:
    wanted = _basename(href)
    if not wanted:
        return ""
    exact = { _basename(url): url for url in media_urls }
    if wanted in exact:
        return exact[wanted]
    wanted_stem = Path(wanted).stem
    for name, url in exact.items():
        if Path(name).stem == wanted_stem:
            return url
    return ""


def select_pmc_figures(xml_text: str,
                       media_urls: Iterable[object]) -> list[dict]:
    """Return one safe raster candidate per JATS figure, in preference order."""
    safe_media = [url for value in media_urls if (url := pmc_media_url(value))]
    if not safe_media:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    candidates = []
    for order, fig in enumerate(node for node in root.iter()
                                if _local_name(node.tag) == "fig"):
        caption_node = next((node for node in fig.iter()
                             if _local_name(node.tag) == "caption"), None)
        label_node = next((node for node in fig.iter()
                           if _local_name(node.tag) == "label"), None)
        caption = _node_text(caption_node)
        label = _node_text(label_node)
        if not visual_policy.has_reviewable_figure_caption(caption):
            continue
        # Rights/credit statements may sit in ``attrib`` or another child of
        # ``fig`` rather than inside the caption itself.
        if visual_policy.has_third_party_figure_rights(_node_text(fig)):
            continue
        image_url = _match_media(_graphic_href(fig), safe_media)
        if not image_url:
            continue
        kind = (fig.attrib.get("fig-type") or "").lower()
        figure_id = (fig.attrib.get("id") or "").lower()
        media_name = _basename(image_url)
        if looks_like_auxiliary_image(media_name, label):
            continue
        if looks_like_decorative_image(
                kind, figure_id, media_name, label, caption):
            continue
        haystack = f"{kind} {figure_id} {media_name} {label} {caption}".lower()
        preferred = int(
            "graphical abstract" in haystack or
            "graphical-abstract" in haystack or
            media_name.startswith(("ga", "fx"))
        )
        unnumbered = int(
            kind in {"unnumbered", "undfig"} or figure_id.startswith("undfig")
        )
        display_label = label or (
            "Graphical abstract" if preferred else "Figure"
        )
        candidates.append((-(preferred * 2 + unnumbered), order, {
            "image_url": image_url,
            "caption": caption,
            "label": display_label,
        }))
    return [candidate for _rank, _order, candidate in sorted(candidates)]


def select_pmc_figure(xml_text: str, media_urls: Iterable[object]) -> dict | None:
    """Compatibility wrapper returning the preferred safe JATS figure."""
    return next(iter(select_pmc_figures(xml_text, media_urls)), None)


def _direct_child(node: ET.Element | None, name: str) -> ET.Element | None:
    if node is None:
        return None
    return next((child for child in node
                 if _local_name(child.tag) == name), None)


def _jats_article_meta(root: ET.Element) -> ET.Element | None:
    return _direct_child(_direct_child(root, "front"), "article-meta")


def _jats_article_dois(root: ET.Element) -> set[str]:
    article_meta = _jats_article_meta(root)
    return {
        _node_text(node).lower()
        for node in (article_meta or [])
        if (_local_name(node.tag) == "article-id" and
            str(node.attrib.get("pub-id-type") or "").lower() == "doi" and
            _node_text(node))
    }


def _jats_mdpi_publisher(root: ET.Element) -> bool:
    journal_meta = _direct_child(_direct_child(root, "front"), "journal-meta")
    publisher = _direct_child(journal_meta, "publisher")
    names = {
        _node_text(node).strip().lower()
        for node in (publisher or [])
        if _local_name(node.tag) == "publisher-name" and _node_text(node)
    }
    return bool(names) and all(name in {"mdpi", "mdpi ag"} for name in names)


def _jats_license_names(root: ET.Element) -> set[str] | None:
    """Return one fully allowlisted licence set; omissions fail closed.

    A present licence node fails closed unless every declared link is the same
    exact allowlisted Creative Commons URL.  Older MDPI JATS legitimately uses
    canonical HTTP CC links, and a small legacy subset has no ``href``; those
    must contain exactly one complete CC URL in the licence prose.  Crossref's
    authoritative layer remains HTTPS-only.
    """
    article_meta = _jats_article_meta(root)
    permissions = _direct_child(article_meta, "permissions")
    license_nodes = [node for node in (permissions or [])
                     if _local_name(node.tag) == "license"]
    if not license_nodes:
        return set()
    names = set()
    for license_node in license_nodes:
        license_text = _node_text(license_node)
        if _jats_license_text_has_restrictions(license_text):
            return set()
        hrefs = []
        for node in license_node.iter():
            for key, value in node.attrib.items():
                if key == "href" or key.endswith("}href"):
                    hrefs.append(value)
        text_urls = _text_http_urls(license_text)
        declared_urls = hrefs if hrefs else text_urls
        # A no-href legacy node must have exactly one complete URL.  When
        # hrefs exist, prose URLs are corroborating declarations too and may
        # not introduce another licence or unrelated destination.
        if not declared_urls or (not hrefs and len(text_urls) != 1):
            return set()
        if hrefs:
            declared_urls = hrefs + text_urls
        node_names = set()
        for href in declared_urls:
            name = _creative_commons_license_url(href, allow_http=True)
            if not name:
                return set()
            node_names.add(name)
        if len(node_names) != 1:
            return set()
        names.update(node_names)
    return names if len(names) == 1 else set()


def select_mdpi_figures(xml_text: str, context: dict) -> tuple[list[dict], str]:
    """Validate one exact MDPI JATS article and derive safe raster candidates.

    Absolute or relative asset URLs from XML are never trusted.  Only a plain
    ``{article-stem}-gNNN`` graphic filename is used to construct audited
    browser-image locations under the same official article deployment.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return [], "invalid_xml"
    if (_local_name(root.tag) != "article" or
            _jats_article_dois(root) != {str(context["doi"]).lower()} or
            not _jats_mdpi_publisher(root)):
        return [], "identity_mismatch"

    jats_licenses = _jats_license_names(root)
    if jats_licenses == set():
        return [], "license_not_reusable"
    if (jats_licenses is not None and
            jats_licenses != {str(context.get("license_name") or "")}):
        return [], "license_not_reusable"

    ranked = []
    stem = str(context["stem"])
    slug = str(context["slug"])
    graphic_pattern = re.compile(
        rf"({re.escape(stem)}-g\d{{3}})\.(?:tiff?|png|jpe?g)",
        flags=re.IGNORECASE,
    )
    for order, fig in enumerate(node for node in root.iter()
                                if _local_name(node.tag) == "fig"):
        caption_node = next((node for node in fig.iter()
                             if _local_name(node.tag) == "caption"), None)
        label_node = next((node for node in fig.iter()
                           if _local_name(node.tag) == "label"), None)
        caption = _node_text(caption_node)
        label = _node_text(label_node)
        if not visual_policy.has_reviewable_figure_caption(caption):
            continue
        if visual_policy.has_third_party_figure_rights(_node_text(fig)):
            continue
        href = _graphic_href(fig).strip()
        match = graphic_pattern.fullmatch(href)
        if not match:
            continue
        graphic_stem = match.group(1).lower()
        kind = str(fig.attrib.get("fig-type") or "").lower()
        figure_id = str(fig.attrib.get("id") or "").lower()
        if looks_like_auxiliary_image(graphic_stem, label):
            continue
        if looks_like_decorative_image(
                kind, figure_id, graphic_stem, label, caption):
            continue
        haystack = f"{kind} {figure_id} {label} {caption}".lower()
        preferred = int(
            "graphical abstract" in haystack or
            "graphical-abstract" in haystack
        )
        base = (
            f"{MDPI_ASSET_ROOT}/{slug}/{stem}/article_deploy/html/images/"
            f"{graphic_stem}"
        )
        display_label = label or (
            "Graphical abstract" if preferred else "Figure"
        )
        for format_order, image_url in enumerate((
                f"{base}-550.jpg", f"{base}.png")):
            if not _mdpi_asset_url(image_url, context):
                continue
            ranked.append((-preferred, order, format_order, {
                "image_url": image_url,
                "caption": caption,
                "label": display_label,
            }))
    return [item for _preferred, _order, _format, item in sorted(ranked)], ""


def _pmc_versions(list_xml: str, pmcid: str) -> list[str]:
    try:
        root = ET.fromstring(list_xml)
    except ET.ParseError as exc:
        raise FetchError("PMC S3 returned invalid listing XML") from exc
    pattern = re.compile(rf"^{re.escape(pmcid)}\.(\d+)/$")
    found = []
    for node in root.iter():
        if _local_name(node.tag) != "prefix":
            continue
        value = (node.text or "").strip()
        match = pattern.match(value)
        if match:
            found.append((int(match.group(1)), value.rstrip("/")))
    return [value for _, value in sorted(found, reverse=True)]


class ArxivFigureParser(HTMLParser):
    """Small parser for official arXiv HTML ``figure`` elements."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.caption_depth = 0
        self.current: dict | None = None
        self.figures: list[dict] = []
        self.figure_contexts: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "figure":
            if self.depth == 0:
                self.current = {"images": [], "graphics": [],
                                "caption_parts": [], "text_parts": [],
                                "class": attributes.get("class") or ""}
            self.figure_contexts.append({
                "class": attributes.get("class") or "",
                "id": attributes.get("id") or "",
            })
            self.depth += 1
            return
        if self.depth and tag == "img" and self.current is not None:
            context = " ".join(
                f"{item['class']} {item['id']}"
                for item in self.figure_contexts
            ).lower()
            src = attributes.get("src") or ""
            is_raster = bool(src) and _suffix(src) in IMAGE_SUFFIXES
            graphic_order = (
                len(self.current["graphics"]) if is_raster else None
            )
            image = {
                "src": src,
                "alt": attributes.get("alt") or "",
                "class": attributes.get("class") or "",
                "id": attributes.get("id") or "",
                "width": attributes.get("width") or "",
                "height": attributes.get("height") or "",
                "graphic_order": graphic_order,
                "is_subfigure": bool(re.search(
                    r"(?:^|[\s_.-])(?:subfig(?:ure)?|figure_panel|panel)"
                    r"(?:$|[\s_.-])",
                    context,
                )),
            }
            self.current["images"].append(image)
            if is_raster:
                self.current["graphics"].append({
                    "kind": "img", "src": image["src"],
                    "width": image["width"], "height": image["height"],
                })
        if self.depth and tag == "object" and self.current is not None:
            data = attributes.get("data") or ""
            media_type = (attributes.get("type") or "").lower()
            if data and (media_type.startswith("image/") or
                         _suffix(data) == ".svg"):
                context = " ".join(
                    f"{item['class']} {item['id']}"
                    for item in self.figure_contexts
                ).lower()
                object_label = " ".join(
                    " ".join(str(attributes.get(name) or "").split())
                    for name in ("title", "aria-label")
                    if str(attributes.get(name) or "").strip()
                )
                self.current["graphics"].append({
                    "kind": "object", "src": data,
                    "alt": object_label,
                    "width": attributes.get("width") or "",
                    "height": attributes.get("height") or "",
                    "graphic_order": len(self.current["graphics"]),
                    "is_subfigure": bool(re.search(
                        r"(?:^|[\s_.-])(?:subfig(?:ure)?|figure_panel|panel)"
                        r"(?:$|[\s_.-])",
                        context,
                    )),
                })
        if self.depth and tag == "figcaption":
            self.caption_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "figcaption" and self.caption_depth:
            self.caption_depth -= 1
        if tag == "figure" and self.depth:
            self.depth -= 1
            if self.figure_contexts:
                self.figure_contexts.pop()
            if self.depth == 0 and self.current is not None:
                self.current["caption"] = " ".join(
                    " ".join(self.current.pop("caption_parts")).split()
                )
                self.current["figure_text"] = " ".join(
                    " ".join(self.current.pop("text_parts")).split()
                )
                self.figures.append(self.current)
                self.current = None

    def handle_data(self, data: str) -> None:
        if self.depth and self.current is not None:
            self.current["text_parts"].append(data)
            if self.caption_depth:
                self.current["caption_parts"].append(data)


def _html_image_dimension(value: object) -> float:
    """Parse a conservative numeric HTML image dimension, or return zero."""
    raw = str(value or "").strip().lower()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:px|pt)?", raw)
    if not match:
        return 0.0
    dimension = float(match.group(1))
    return dimension if 0 < dimension <= 100_000 else 0.0


def _looks_like_geometric_auxiliary(
        image: dict, graphics: Iterable[dict],
) -> bool:
    """Identify a tiny extreme strip only beside a more complete graphic.

    Aspect ratio alone is not evidence: portrait scientific fields and wide
    timelines can be legitimate.  The candidate must also be small in both
    absolute area and short edge, and the same outer ``figure`` must contain a
    substantially larger graphic.  That companion may be an SVG ``object``;
    it is selection evidence only and never crosses the raster/host boundary.
    """
    width = _html_image_dimension(image.get("width"))
    height = _html_image_dimension(image.get("height"))
    if not width or not height:
        return False
    short, long = sorted((width, height))
    area = width * height
    if not (long / short >= 4.0 and short <= 64 and
            long <= 512 and area <= 16_000):
        return False

    own_order = image.get("graphic_order")
    for order, graphic in enumerate(graphics):
        if order == own_order:
            continue
        other_width = _html_image_dimension(graphic.get("width"))
        other_height = _html_image_dimension(graphic.get("height"))
        if not other_width or not other_height:
            continue
        other_short = min(other_width, other_height)
        other_area = other_width * other_height
        if other_short >= max(128, short * 2) and other_area >= area * 4:
            return True
    return False


def select_arxiv_figures(html_text: str, page_url: str) -> list[dict]:
    """Return safe arXiv candidates with every raster ahead of SVG fallback."""
    parser = ArxivFigureParser()
    parser.feed(html_text)
    raster_safe = []
    svg_safe = []
    for order, figure in enumerate(parser.figures):
        # LaTeXML represents semantic tables as outer ``figure`` elements.
        # Raster thumbnails inside their cells inherit the Table caption but
        # are not standalone paper figures suitable for a visual card.  Use
        # the converter's structural class rather than an image-size guess;
        # ordinary scientific figures may legitimately be small or narrow.
        outer_classes = set(str(figure.get("class") or "").lower().split())
        if "ltx_table" in outer_classes:
            continue
        caption = figure.get("caption") or ""
        if not visual_policy.has_reviewable_figure_caption(caption):
            continue
        figure_text = str(figure.get("figure_text") or "")
        object_labels = tuple(
            str(graphic.get("alt") or "")
            for graphic in figure.get("graphics") or []
            if graphic.get("kind") == "object"
        )
        if visual_policy.has_third_party_figure_rights(
                figure_text, *(image.get("alt") for image in
                               figure.get("images") or [])):
            continue
        # Object-only labels did not participate in v7 raster selection.
        # Keep them scoped to the SVG fallback so a risky SVG sibling cannot
        # remove an otherwise eligible, previously selected raster figure.
        svg_rights_risk = (
            visual_policy.has_third_party_figure_rights(*object_labels) or
            any(re.search(
                r"\badopted\s+from\b", value, flags=re.IGNORECASE,
            ) for value in (figure_text, *object_labels))
        )
        images = []
        for image_order, image in enumerate(figure.get("images") or []):
            image_url = _https_url(
                # arXiv serves ``/html/<id>`` as a file-like URL.  Relative
                # image paths therefore resolve against ``/html/``.
                urljoin(page_url, image.get("src") or ""),
                hosts={"arxiv.org"},
            )
            if not image_url or _suffix(image_url) not in IMAGE_SUFFIXES:
                continue
            if looks_like_auxiliary_image(
                    image_url, image.get("alt"), image.get("class")):
                continue
            if _looks_like_geometric_auxiliary(
                    image, figure.get("graphics") or []):
                continue
            if looks_like_decorative_image(
                    _basename(image_url), image.get("alt"),
                    caption):
                continue
            images.append({
                "image_url": image_url,
                "alt": image.get("alt") or "",
                "is_subfigure": bool(image.get("is_subfigure")),
                "order": image_order,
            })
        haystack = f"{figure.get('class', '')} {caption}".lower()
        preferred = int("graphical abstract" in haystack)
        if images:
            # A standalone non-panel image represents the complete figure and
            # is preferred over nested panels.  If an author supplies only
            # panels, retain the first eligible panel in document order.
            full_images = [
                image for image in images if not image["is_subfigure"]
            ]
            pool = full_images or images
            selected = min(pool, key=lambda image: image["order"])
            raster_safe.append((-preferred, order, {
                "image_url": selected["image_url"],
                "caption": caption,
                "alt": selected["alt"],
            }))

        svg_objects = []
        if svg_rights_risk:
            continue
        page_work_id = _arxiv_html_work_id(page_url)
        for graphic_order, graphic in enumerate(figure.get("graphics") or []):
            if graphic.get("kind") != "object":
                continue
            raw_src = str(graphic.get("src") or "")
            # LaTeXML usually emits a version-prefixed path, for which arXiv's
            # HTML URL behaves file-like.  A few pages emit a plain relative
            # object path; only for SVG, retry against the versioned paper
            # directory.  Raster resolution above remains byte-for-byte v7.
            image_url = _arxiv_svg_url(urljoin(page_url, raw_src))
            if not image_url:
                image_url = _arxiv_svg_url(urljoin(
                    f"{page_url.rstrip('/')}/", raw_src,
                ))
            if (not image_url or not page_work_id or
                    _arxiv_html_work_id(image_url) != page_work_id):
                continue
            if looks_like_auxiliary_image(image_url):
                continue
            svg_graphic = dict(graphic)
            svg_graphic.setdefault("graphic_order", graphic_order)
            if _looks_like_geometric_auxiliary(
                    svg_graphic, figure.get("graphics") or []):
                continue
            if looks_like_decorative_image(_basename(image_url), caption):
                continue
            svg_objects.append({
                "image_url": image_url,
                "is_subfigure": bool(graphic.get("is_subfigure")),
                "order": graphic_order,
            })
        if svg_objects:
            full_objects = [
                graphic for graphic in svg_objects
                if not graphic["is_subfigure"]
            ]
            pool = full_objects or svg_objects
            selected = min(pool, key=lambda graphic: graphic["order"])
            svg_safe.append((-preferred, order, {
                "image_url": selected["image_url"],
                "caption": caption,
                "alt": caption,
                "media_type": SVG_MEDIA_TYPE,
            }))

    # Raster behavior remains the first choice.  SVG is reached only when all
    # earlier raster candidates are too small or absent, within the same
    # six-fetch budget enforced by the resolver.
    ranked_rasters = [
        candidate for _rank, _order, candidate in sorted(raster_safe)
    ]
    ranked_svgs = [
        candidate for _rank, _order, candidate in sorted(svg_safe)
    ]
    return ranked_rasters + ranked_svgs


def select_arxiv_figure(html_text: str, page_url: str) -> dict | None:
    """Compatibility wrapper returning the preferred safe arXiv figure."""
    return next(iter(select_arxiv_figures(html_text, page_url)), None)


class VisualResolver:
    def __init__(self, client: HttpClient, *, email: str = ""):
        self.client = client
        self.email = email.strip()

    def _verified_card_candidate(
            self, candidates: Iterable[dict], *,
            allowed_hosts: set[str]) -> tuple[dict, int, int] | None:
        """Return the first card-sized candidate after at most six fetches."""
        seen_urls = set()
        checked = 0
        for candidate in candidates:
            image_url = str(candidate.get("image_url") or "")
            if not image_url or image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            if checked >= MAX_IMAGE_CANDIDATES:
                break
            checked += 1
            # Transport, HTTP, and response-size failures remain transient and
            # preserve last-known-good.  A fully downloaded SVG that fails the
            # passive-content policy is deterministic and may fall through to
            # the next candidate.
            try:
                if candidate.get("media_type") == SVG_MEDIA_TYPE:
                    width, height = self.client.verify_svg(
                        image_url, allowed_hosts=allowed_hosts,
                    )
                else:
                    width, height = self.client.verify_image(
                        image_url, allowed_hosts=allowed_hosts,
                    )
            except SvgValidationError:
                # Active, externally-referencing, malformed, or otherwise
                # non-passive SVG is a deterministic candidate rejection.
                # Network/HTTP/size failures remain ordinary FetchError and
                # still preserve last-known-good through the outer resolver.
                continue
            if width * height < MIN_CARD_IMAGE_PIXELS:
                continue
            return candidate, width, height
        return None

    def _verified_mdpi_candidate(
            self, candidates: Iterable[dict]) -> tuple[dict, int, int] | None:
        """Return the first card-sized MDPI raster within the shared budget."""
        seen_urls = set()
        checked = 0
        for candidate in candidates:
            image_url = str(candidate.get("image_url") or "")
            if not image_url or image_url in seen_urls:
                continue
            seen_urls.add(image_url)
            if checked >= MAX_IMAGE_CANDIDATES:
                break
            checked += 1
            try:
                width, height = self.client.verify_mdpi_image(image_url)
            except MdpiImageValidationError:
                # A 404/malformed body/wrong MIME/signature is deterministic
                # for the derived format and may fall through to PNG/next fig.
                continue
            except FetchError as exc:
                if _http_not_found(exc):
                    continue
                raise
            if width * height < MIN_CARD_IMAGE_PIXELS:
                continue
            return candidate, width, height
        return None

    def _pmcid(self, paper: dict) -> str:
        requested = str(paper.get("pmid") or "").strip()
        if not requested:
            requested = str(paper.get("doi") or "").strip()
        if not requested:
            return ""
        params = {
            "ids": requested,
            "format": "json",
            "tool": "research-radar-visuals",
        }
        if self.email:
            params["email"] = self.email
        payload = self.client.get_json(
            ID_CONVERTER_URL,
            params=params,
            allowed_hosts={"pmc.ncbi.nlm.nih.gov"},
        )
        for record in payload.get("records") or []:
            if isinstance(record, dict) and record.get("pmcid"):
                pmcid = str(record["pmcid"]).upper()
                return pmcid if re.fullmatch(r"PMC\d+", pmcid) else ""
        return ""

    def resolve_pmc(self, paper: dict, checked_at: str) -> dict | None:
        if not (paper.get("doi") or paper.get("pmid")):
            return None
        pmcid = self._pmcid(paper)
        if not pmcid:
            return None
        listing = self.client.get_text(
            PMC_BUCKET_URL,
            params={"list-type": "2", "prefix": f"{pmcid}.", "delimiter": "/"},
            allowed_hosts={PMC_BUCKET_HOST},
        )
        versions = _pmc_versions(listing, pmcid)
        if not versions:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="pmc_version_not_available", provider="pmc",
            )
        version = versions[0]
        metadata = self.client.get_json(
            f"{PMC_BUCKET_URL}metadata/{version}.json",
            allowed_hosts={PMC_BUCKET_HOST},
        )
        license_name = normalize_license(metadata.get("license_code"))
        if license_name not in ALLOWED_LICENSES:
            return _blank_visual(
                "blocked", checked_at=checked_at,
                reason="pmc_license_not_reusable", license_name=license_name,
                provider="pmc",
            )
        xml_url = _https_url(metadata.get("xml_url"), hosts={PMC_BUCKET_HOST})
        if not xml_url:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="pmc_jats_not_available", license_name=license_name,
                provider="pmc",
            )
        xml_text = self.client.get_text(
            xml_url, allowed_hosts={PMC_BUCKET_HOST},
        )
        candidates = select_pmc_figures(
            xml_text, metadata.get("media_urls") or [],
        )
        if not candidates:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="pmc_no_reusable_figure", license_name=license_name,
                provider="pmc",
            )
        verified = self._verified_card_candidate(
            candidates, allowed_hosts={PMC_BUCKET_HOST},
        )
        if not verified:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="pmc_no_card_sized_figure", license_name=license_name,
                provider="pmc",
            )
        selected, width, height = verified
        source_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
        return _available_visual(
            checked_at=checked_at,
            image_url=selected["image_url"], caption=selected["caption"],
            source_label=f"PubMed Central · {selected['label']}",
            source_url=source_url, license_name=license_name,
            alt=selected["caption"] or paper.get("title") or "PMC 论文插图",
            provider="pmc", width=width, height=height,
        )

    def resolve_arxiv(self, paper: dict, checked_at: str) -> dict | None:
        raw_id = str(paper.get("arxiv_id") or "").strip()
        if not raw_id:
            return None
        base_id = re.sub(r"v\d+$", "", raw_id, flags=re.IGNORECASE)
        versioned_id = raw_id if re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})v[1-9]\d*",
            raw_id,
            flags=re.IGNORECASE,
        ) else ""
        oai = self.client.get_text(
            ARXIV_OAI_URL,
            params={
                "verb": "GetRecord",
                "identifier": f"oai:arXiv.org:{base_id}",
                "metadataPrefix": "arXivRaw",
            },
            allowed_hosts=ARXIV_OAI_HOSTS,
        )
        try:
            root = ET.fromstring(oai)
        except ET.ParseError as exc:
            raise FetchError("arXiv OAI returned invalid XML") from exc
        license_value = ""
        for node in root.iter():
            if _local_name(node.tag) == "license" and (node.text or "").strip():
                license_value = (node.text or "").strip()
                break
        license_name = normalize_license(license_value)
        if license_name not in ALLOWED_LICENSES:
            return _blank_visual(
                "blocked", checked_at=checked_at,
                reason="arxiv_license_not_reusable", license_name=license_name,
                provider="arxiv",
            )
        # Pin official HTML assets to a paper version when the corpus supplies
        # one.  Unversioned records retain raster behavior, while the strict
        # SVG boundary rejects their unstable relative object URLs.
        page_url = f"https://arxiv.org/html/{versioned_id or base_id}"
        html_text = self.client.get_text(
            page_url, allowed_hosts={"arxiv.org"},
        )
        candidates = select_arxiv_figures(html_text, page_url)
        if not candidates:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="arxiv_html_no_reusable_figure",
                license_name=license_name, provider="arxiv",
            )
        verified = self._verified_card_candidate(
            candidates, allowed_hosts={"arxiv.org"},
        )
        if not verified:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="arxiv_no_card_sized_figure",
                license_name=license_name, provider="arxiv",
            )
        selected, width, height = verified
        return _available_visual(
            checked_at=checked_at, image_url=selected["image_url"],
            caption=selected["caption"], source_label="arXiv · 论文插图",
            source_url=f"https://arxiv.org/abs/{base_id}",
            license_name=license_name,
            alt=selected.get("alt") or selected["caption"] or
                paper.get("title") or "arXiv 论文插图",
            provider="arxiv", width=width, height=height,
            media_type=str(selected.get("media_type") or ""),
        )

    def resolve_mdpi(self, paper: dict, checked_at: str) -> dict | None:
        doi_context = _mdpi_doi_context(paper.get("doi"))
        if doi_context is None:
            return None
        requested_doi = str(doi_context["doi"])
        try:
            metadata = self.client.get_crossref_work(requested_doi)
        except FetchError as exc:
            if _http_not_found(exc):
                return _blank_visual(
                    "not_found", checked_at=checked_at,
                    reason="mdpi_metadata_not_available", provider="mdpi",
                )
            raise
        if str(metadata.get("DOI") or "").strip().lower() != requested_doi:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_metadata_not_available", provider="mdpi",
            )
        publisher = " ".join(str(metadata.get("publisher") or "").split())
        if publisher.lower() not in {"mdpi", "mdpi ag"}:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_metadata_not_available", provider="mdpi",
            )

        as_of = parse_timestamp(checked_at)
        license_name = (
            _crossref_vor_license_name(metadata, as_of)
            if as_of is not None else ""
        )
        if not license_name:
            return _blank_visual(
                "blocked", checked_at=checked_at,
                reason="mdpi_license_not_reusable",
                provider="mdpi",
            )
        primary = metadata.get("resource") or {}
        primary = primary.get("primary") if isinstance(primary, dict) else {}
        source_url = primary.get("URL") if isinstance(primary, dict) else ""
        context = _mdpi_source_context(source_url, doi_context)
        if context is None:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_metadata_not_available",
                license_name=license_name, provider="mdpi",
            )
        context["license_name"] = license_name

        xml_url = _mdpi_xml_url(context)
        try:
            xml_text = self.client.get_mdpi_xml(xml_url)
        except FetchError as exc:
            if _http_not_found(exc):
                return _blank_visual(
                    "not_found", checked_at=checked_at,
                    reason="mdpi_jats_not_available",
                    license_name=license_name, provider="mdpi",
                )
            raise
        candidates, rejection = select_mdpi_figures(xml_text, context)
        if rejection == "license_not_reusable":
            return _blank_visual(
                "blocked", checked_at=checked_at,
                reason="mdpi_license_not_reusable",
                license_name=license_name, provider="mdpi",
            )
        if rejection in {"invalid_xml", "identity_mismatch"}:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_jats_not_available",
                license_name=license_name, provider="mdpi",
            )
        if not candidates:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_no_reusable_figure",
                license_name=license_name, provider="mdpi",
            )
        verified = self._verified_mdpi_candidate(candidates)
        if not verified:
            return _blank_visual(
                "not_found", checked_at=checked_at,
                reason="mdpi_no_card_sized_figure",
                license_name=license_name, provider="mdpi",
            )
        selected, width, height = verified
        return _available_visual(
            checked_at=checked_at, image_url=selected["image_url"],
            caption=selected["caption"],
            source_label=f"MDPI · {selected['label']}",
            source_url=str(context["source_url"]),
            license_name=license_name,
            alt=selected["caption"] or paper.get("title") or "MDPI 论文插图",
            provider="mdpi", width=width, height=height,
        )

    def resolve(self, paper: dict, *, now: dt.datetime | None = None) -> dict:
        checked_at = iso_z(now or utc_now())
        # PMC has structured media, figure captions, and article-level licence
        # metadata, so it is always preferred when a DOI/PMID maps there.
        errors = []
        try:
            pmc = self.resolve_pmc(paper, checked_at)
        except FetchError as exc:
            pmc = None
            errors.append(f"pmc: {exc}")
        if pmc is not None and pmc.get("status") == "available":
            return pmc
        try:
            arxiv = self.resolve_arxiv(paper, checked_at)
        except FetchError as exc:
            arxiv = None
            errors.append(f"arxiv: {exc}")
        if arxiv is not None and arxiv.get("status") == "available":
            return arxiv
        # Preserve the complete PMC/arXiv result precedence.  MDPI is a
        # deliberately narrow extension of the former source-less branch,
        # never an override for an existing provider result or error.
        if pmc is not None and pmc.get("status") == "blocked":
            return pmc
        if arxiv is not None:
            return arxiv
        if pmc is not None:
            return pmc
        if errors:
            return _blank_visual(
                "error", checked_at=checked_at,
                reason="; ".join(errors)[:240],
            )
        try:
            mdpi = self.resolve_mdpi(paper, checked_at)
        except FetchError as exc:
            return _blank_visual(
                "error", checked_at=checked_at,
                reason=f"mdpi: {exc}"[:240], provider="mdpi",
            )
        if mdpi is not None:
            return mdpi
        return _blank_visual(
            "not_found", checked_at=checked_at,
            reason="no_supported_public_figure_source",
        )

def should_refresh(record: object, *, now: dt.datetime | None = None,
                   force: bool = False) -> bool:
    if force or not isinstance(record, dict):
        return True
    if (record.get("status") == "not_found" and
            record.get("reason") in {
                "arxiv_html_no_reusable_figure",
                "arxiv_no_card_sized_figure",
            } and (
                not isinstance(record.get("selector_version"), int) or
                record.get("selector_version") < ARXIV_SVG_SELECTOR_VERSION
            )):
        # Selector v8 adds independently hosted, strictly validated SVG
        # objects.  Retry each old arXiv negative once without waiting for its
        # 30-day TTL; the refreshed blank also records v8 and resumes caching.
        return True
    selector_version = record.get("selector_version")
    if (record.get("status") == "available" and
            (not isinstance(selector_version, int) or
             selector_version < MIN_CURRENT_AVAILABLE_SELECTOR_VERSION)):
        last_error = parse_timestamp(record.get("selector_error_at"))
        if (last_error is not None and
                (now or utc_now()) - last_error < dt.timedelta(days=1)):
            return False
        return True
    checked = parse_timestamp(record.get("checked_at"))
    if checked is None:
        return True
    age = (now or utc_now()) - checked
    status = record.get("status")
    ttl = {
        "available": dt.timedelta(days=180),
        "blocked": dt.timedelta(days=180),
        "not_found": dt.timedelta(days=30),
        "error": dt.timedelta(days=1),
    }.get(status, dt.timedelta(0))
    return age >= ttl


def _sort_candidates(
        candidates: list[tuple[str, dict, str]],
) -> list[tuple[str, dict, str]]:
    order = {"High": 0, "Medium": 1}
    # Stable two-pass sort: newest Radar discovery first, then High before
    # Medium within the same discovery time. Publication buckets may be dated
    # in the future and therefore are not a reliable recency signal.
    candidates.sort(key=lambda item: (
        order.get(str((item[1].get("llm") or {}).get("priority")), 9),
        item[0],
    ))
    candidates.sort(
        key=lambda item: str(item[1].get("first_seen_at") or item[2]),
        reverse=True,
    )
    return candidates


def _fresh_alias_record(
        entries: Iterable[tuple[str, object]], *, now: dt.datetime,
        force: bool) -> dict | None:
    """Choose one reusable fresh cache record for a lookup alias group."""
    if force:
        return None
    fresh = [
        record for _key, record in entries
        if isinstance(record, dict)
        and not should_refresh(record, now=now)
    ]
    if not fresh:
        return None
    status_rank = {"available": 3, "blocked": 2, "not_found": 1, "error": 0}
    return max(fresh, key=lambda record: (
        status_rank.get(str(record.get("status") or ""), -1),
        str(record.get("checked_at") or ""),
    ))


def _last_available_record(
        entries: Iterable[tuple[str, object]]) -> dict | None:
    """Return last-known-good display data for transient-error fallback."""
    available = [
        record for _key, record in entries
        if isinstance(record, dict) and record.get("status") == "available"
    ]
    return max(
        available,
        key=lambda record: str(record.get("checked_at") or ""),
        default=None,
    )


def _store_aliases(records: dict, aliases: Iterable[str], visual: dict) -> bool:
    """Copy one result to exact renderer keys and report whether it changed."""
    changed = False
    for alias in aliases:
        clone = dict(visual)
        if records.get(alias) != clone:
            records[alias] = clone
            changed = True
    return changed


def candidates_from_corpus(papers: Iterable[dict], priorities: set[str],
                           identities: set[str] | None = None
                           ) -> list[tuple[str, dict, str]]:
    """``[(identity_key, paper, date)]`` for merged corpus records in the
    wanted priorities, newest Radar discovery first (then High before
    Medium)."""
    out: list[tuple[str, dict, str]] = []
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        key = identity_key(paper)
        if not key:
            continue
        if str((paper.get("llm") or {}).get("priority") or "") not in priorities:
            continue
        if identities is not None and key not in identities:
            continue
        out.append((key, paper, str(paper.get("date") or "")))
    return _sort_candidates(out)


def enrich(*, candidates: list[tuple[str, dict, str]], registry: dict[str, dict],
           resolver: VisualResolver, limit: int, force: bool = False,
           now: dt.datetime | None = None) -> dict:
    """Resolve up to ``limit`` candidates whose cached result is stale.

    ``registry`` maps exact identity keys to their latest visual record (see
    ``radar.store.visuals.latest_visuals``). Nothing is written here: the
    result's ``changed`` holds ``exact key -> visual`` for the caller to
    append. A fresh cached result under the same normalised lookup (DOI case,
    arXiv version) is reused and copied to the exact key; a transient error
    preserves the last known-good visual; one provider failure never stops
    the batch.
    """
    current_time = now or utc_now()
    cached_by_lookup: dict[str, list[tuple[str, object]]] = {}
    for exact_key, record in registry.items():
        lookup = _visual_lookup_identity(exact_key) or exact_key
        cached_by_lookup.setdefault(lookup, []).append((exact_key, record))

    attempted = 0
    counts: dict[str, int] = {}
    changed: dict[str, dict] = {}
    processed: set[str] = set()
    for key, paper, _date in candidates:
        lookup = visual_lookup_key(paper) or key
        if lookup in processed:
            continue
        processed.add(lookup)
        cached_entries = cached_by_lookup.get(lookup, [])
        cached = _fresh_alias_record(cached_entries, now=current_time, force=force)
        newly_supported_mdpi = (
            cached is not None and
            cached.get("status") == "not_found" and
            cached.get("reason") == "no_supported_public_figure_source" and
            _mdpi_doi_context(paper.get("doi")) is not None and
            (not isinstance(cached.get("selector_version"), int) or
             cached.get("selector_version") < MDPI_SELECTOR_VERSION)
        )
        if cached is not None and not newly_supported_mdpi:
            if registry.get(key) != cached:
                changed[key] = dict(cached)   # an alias result the exact key lacked
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            visual = resolver.resolve(paper, now=current_time)
        except Exception as exc:  # one provider/paper must never stop the batch
            visual = _blank_visual("error", checked_at=iso_z(current_time),
                                   reason=f"{type(exc).__name__}: {str(exc)[:240]}")
        status = str(visual.get("status") or "error")
        previous_available = _last_available_record(cached_entries)
        if status == "error" and previous_available is not None:
            preserved = dict(previous_available)
            preserved["selector_error_at"] = iso_z(current_time)
            preserved["selector_error_reason"] = str(visual.get("reason") or "transient resolver error")[:240]
            visual = preserved
            status = "preserved_available"
        changed[key] = dict(visual)
        counts[status] = counts.get(status, 0) + 1
    return {"attempted": attempted, "counts": counts, "changed": changed}
