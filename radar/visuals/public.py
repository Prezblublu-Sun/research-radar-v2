"""The browser-safe projection of a visual record (ported from v1 build_pages).

Registry records are enrichment input, not trusted presentation data. Every
record crosses this boundary before it reaches a shard: image hosts are
restricted to the supported providers, licences to the open ones, captions
checked for third-party rights, SVG and MDPI assets re-bound to their source.
"""
from __future__ import annotations

import pathlib
import re
import urllib.parse

from radar.visuals import policy


DAY_PAGE_SIZE = 20
_DAILY_BUCKET_FILENAME = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
_VISUAL_IMAGE_HOSTS = {
    "arxiv.org",
    "export.arxiv.org",
    "mdpi-res.com",
    "pmc-oa-opendata.s3.amazonaws.com",
}
_VISUAL_SVG_MEDIA_TYPE = "image/svg+xml"
_VISUAL_RASTER_MEDIA_BY_SUFFIX = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_ARXIV_SVG_PATH = re.compile(
    r"^/html/(?P<work>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})"
    r"v[1-9]\d*)/"
    r".+\.svg$",
    re.IGNORECASE,
)
_MDPI_ISSN_BY_SLUG = {
    "applsci": "2076-3417",
    "buildings": "2075-5309",
    "coatings": "2079-6412",
    "designs": "2411-9660",
    "jmmp": "2504-4494",
    "metals": "2075-4701",
    "psf": "2673-9984",
}
_MDPI_RASTER_PATH = re.compile(
    r"^/d_attachment/(?P<slug>applsci|buildings|coatings|designs|jmmp|metals|psf)/"
    r"(?P<stem>(?P=slug)-(?P<volume>\d{2})-(?P<article>\d{5}))/"
    r"article_deploy/html/images/(?P=stem)-g(?P<figure>(?!000)\d{3})"
    r"(?:-550\.jpg|\.png)$"
)
_MDPI_SOURCE_PATH = re.compile(
    r"^/(?P<issn>\d{4}-\d{3}[\dXx])/(?P<volume>[1-9]\d*)/"
    r"(?P<issue>[1-9]\d*)/(?P<article>[1-9]\d*)$"
)
_VISUAL_TEXT_LIMITS = {
    "caption": 600,
    "source_label": 80,
    "license": 80,
    "alt": 300,
    "checked_at": 48,
}



def truncate_visual_text(value: str, limit: int) -> str:
    """Bound public visual copy without ending in the middle of a word.

    Figure captions can be very long (and may contain compact LaTeX tokens),
    so a boundary is only used when it is reasonably close to the hard limit.
    This keeps the payload cap deterministic without producing fragments such
    as ``approximat`` in otherwise ordinary prose.
    """
    rendered = " ".join(value.split())
    if len(rendered) <= limit:
        return rendered
    boundary = rendered.rfind(" ", 0, limit)
    if boundary >= max(1, int(limit * 0.75)):
        rendered = rendered[:boundary].rstrip()
    else:
        rendered = rendered[:limit - 1].rstrip()
    return rendered + "…"


def useful_visual_alt(value: object) -> str:
    """Drop provider placeholders that add no information to an image."""
    if not isinstance(value, str):
        return ""
    rendered = " ".join(value.split()).strip()
    if not policy.has_reviewable_figure_caption(rendered):
        return ""
    placeholder = re.fullmatch(
        r"(?:refer|see)\s+(?:to\s+)?(?:the\s+)?caption[.!]?|"
        r"(?:figure|image|graphic)[.!]?",
        rendered,
        flags=re.IGNORECASE,
    )
    return "" if placeholder else rendered


def safe_https_url(value: object, *, image: bool = False) -> str:
    """Return a normalized HTTPS URL accepted at the public-data boundary.

    Figure registries are enrichment input, not trusted presentation data.
    Images are restricted to the two upstream families currently supported by
    the enrichment adapters; source links may point to any HTTPS article page.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (parsed.scheme.lower() != "https" or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or
            port not in (None, 443)):
        return ""
    if image and parsed.hostname.lower() not in _VISUAL_IMAGE_HOSTS:
        return ""
    return urllib.parse.urlunsplit(parsed)


def safe_visual_license(value: object) -> str:
    """Accept only the open licences supported by the visual-enrichment ADR."""
    if not isinstance(value, str):
        return ""
    rendered = value.strip()[:_VISUAL_TEXT_LIMITS["license"]]
    normalized = re.sub(r"[^a-z0-9]+", "-", rendered.lower()).strip("-")
    if re.fullmatch(r"cc0(?:-\d+(?:-\d+)?)?", normalized):
        return rendered
    if re.fullmatch(r"cc-by(?:-sa)?(?:-\d+(?:-\d+)?)?", normalized):
        return rendered
    return ""


def safe_visual_record(value: object) -> dict | None:
    """Fail closed and return the compact visual shape exposed to browsers."""
    if not isinstance(value, dict) or value.get("status") != "available":
        return None
    # Selector v7 no longer emits raster thumbnails from arXiv semantic
    # tables.  Keep the public projection safe immediately after deployment,
    # before every older registry record has been refreshed.  Scope this
    # compatibility boundary to explicit leading table labels and arXiv;
    # ordinary Figure captions may mention a table in scientific prose, and
    # PMC figures use independently structured JATS selection.
    raw_caption = value.get("caption")
    if (value.get("provider") == "arxiv" and
            isinstance(raw_caption, str) and re.match(
                r"^\s*tab(?:le|\.)\s*"
                r"(?:\d+[a-z]?|[ivxlcdm]+)\s*[:.]",
                raw_caption, re.IGNORECASE)):
        return None
    if not policy.has_reviewable_figure_caption(raw_caption):
        return None
    if policy.has_third_party_figure_rights(
            value.get("caption"), value.get("alt"),
            value.get("source_label")):
        return None
    image_url = safe_https_url(value.get("image_url"), image=True)
    source_url = safe_https_url(value.get("source_url"))
    license_name = safe_visual_license(value.get("license"))
    if not image_url or not source_url or not license_name:
        return None

    image_parts = urllib.parse.urlsplit(image_url)
    source_parts = urllib.parse.urlsplit(source_url)
    image_suffix = pathlib.PurePosixPath(
        urllib.parse.unquote(image_parts.path)
    ).suffix.lower()
    raw_media_type = value.get("media_type")
    media_type = (
        raw_media_type.strip().lower()
        if isinstance(raw_media_type, str) else ""
    )
    is_svg = image_suffix == ".svg" or media_type == _VISUAL_SVG_MEDIA_TYPE
    if is_svg:
        if any(
            isinstance(value.get(field), str) and re.search(
                r"\badopted\s+from\b", value[field], re.IGNORECASE,
            )
            for field in ("caption", "alt", "source_label")
        ):
            return None
        path_match = _ARXIV_SVG_PATH.fullmatch(
            urllib.parse.unquote(image_parts.path)
        )
        source_match = re.fullmatch(
            r"/abs/(?P<work>(?:\d{4}\.\d{4,5}|[a-z][a-z0-9.-]*/\d{7})"
            r"(?:v[1-9]\d*)?)",
            urllib.parse.unquote(source_parts.path).rstrip("/"),
            re.IGNORECASE,
        )
        image_work = path_match.group("work") if path_match else ""
        source_work = source_match.group("work") if source_match else ""
        if (
            media_type != _VISUAL_SVG_MEDIA_TYPE or
            value.get("provider") != "arxiv" or
            image_parts.hostname != "arxiv.org" or
            source_parts.hostname != "arxiv.org" or
            image_parts.netloc.lower() != "arxiv.org" or
            source_parts.netloc.lower() != "arxiv.org" or
            image_parts.query or image_parts.fragment or
            source_parts.query or source_parts.fragment or
            "%" in image_parts.path or "\\" in image_parts.path or
            "%" in source_parts.path or "\\" in source_parts.path or
            any(part in {"", ".", ".."} for part in
                image_parts.path[len("/html/"):].split("/")) or
            not path_match or not source_match or
            re.sub(r"v\d+$", "", image_work, flags=re.IGNORECASE).lower() !=
            re.sub(r"v\d+$", "", source_work, flags=re.IGNORECASE).lower()
        ):
            return None
    elif (
        value.get("provider") == "mdpi" or
        image_parts.hostname == "mdpi-res.com" or
        source_parts.hostname == "www.mdpi.com"
    ):
        image_match = _MDPI_RASTER_PATH.fullmatch(image_parts.path)
        source_match = _MDPI_SOURCE_PATH.fullmatch(source_parts.path)
        slug = image_match.group("slug") if image_match else ""
        if (
            value.get("provider") != "mdpi" or
            image_parts.hostname != "mdpi-res.com" or
            source_parts.hostname != "www.mdpi.com" or
            image_parts.netloc.lower() != "mdpi-res.com" or
            source_parts.netloc.lower() != "www.mdpi.com" or
            image_parts.query or image_parts.fragment or
            source_parts.query or source_parts.fragment or
            "%" in image_parts.path or "\\" in image_parts.path or
            "%" in source_parts.path or "\\" in source_parts.path or
            not image_match or not source_match or
            _MDPI_ISSN_BY_SLUG.get(slug) != source_match.group("issn") or
            int(image_match.group("volume")) !=
            int(source_match.group("volume")) or
            int(image_match.group("article")) !=
            int(source_match.group("article"))
        ):
            return None
        expected_media_type = _VISUAL_RASTER_MEDIA_BY_SUFFIX.get(image_suffix)
        if not expected_media_type or (
                media_type and media_type != expected_media_type):
            return None
    elif media_type:
        expected_media_type = _VISUAL_RASTER_MEDIA_BY_SUFFIX.get(image_suffix)
        if not expected_media_type or media_type != expected_media_type:
            return None

    visual = {
        "status": "available",
        "image_url": image_url,
        "source_url": source_url,
        "license": license_name,
    }
    for field in ("caption", "source_label", "alt", "checked_at"):
        raw = value.get(field)
        if field == "alt":
            raw = useful_visual_alt(raw)
        if isinstance(raw, str) and raw.strip():
            limit = _VISUAL_TEXT_LIMITS[field]
            visual[field] = (
                truncate_visual_text(raw, limit)
                if field in {"caption", "alt"}
                else raw.strip()[:limit]
            )
    for field in ("width", "height"):
        raw = value.get(field)
        if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw <= 100_000:
            visual[field] = raw
    if value.get("provider") == "mdpi":
        if ("width" not in visual or "height" not in visual or
                visual["width"] * visual["height"] < 4096):
            return None
        # The lazy renderer needs this one provider discriminator to repeat
        # the server-side image/source work binding. Other resolver internals
        # remain private, and search documents omit the optional visual.
        visual["provider"] = "mdpi"
    if is_svg:
        if ("width" not in visual or "height" not in visual or
                visual["width"] * visual["height"] < 4096):
            return None
        visual["media_type"] = _VISUAL_SVG_MEDIA_TYPE
    elif media_type:
        visual["media_type"] = media_type
    return visual


def registry_records(payload: object) -> dict[str, object]:
    """Tolerate both the v1 keyed registry and an early list-form prototype."""
    if not isinstance(payload, dict):
        return {}
    records = payload.get("records", payload)
    if isinstance(records, dict):
        return {str(key): value for key, value in records.items() if key}
    if isinstance(records, list):
        return {
            str(record["identity_key"]): record
            for record in records
            if isinstance(record, dict) and record.get("identity_key")
        }
    return {}


def safe_registry(records: dict[str, dict]) -> dict[str, dict]:
    """``identity_key -> safe visual`` for every record that passes the boundary."""
    out: dict[str, dict] = {}
    for key, record in records.items():
        safe = safe_visual_record(record)
        if safe is not None:
            out[key] = safe
    return out
