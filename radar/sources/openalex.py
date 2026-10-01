"""OpenAlex fetcher with optional authentication and bounded retries."""

from __future__ import annotations
import os
import datetime as dt
import re
import time
import requests

OPENALEX_BASE = "https://api.openalex.org/works"

# ADR-0031: OpenAlex indexes arXiv preprints without a DOI; the arXiv id is
# only visible in the location URLs (arxiv.org/abs/<id>). Kept local so the
# fetcher layer does not import the render package.
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", re.IGNORECASE)
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)


def _arxiv_id_from_url(url) -> str:
    match = _ARXIV_URL.search(str(url or ""))
    if not match:
        return ""
    ident = match.group(1).strip("/")
    if ident.lower().endswith(".pdf"):
        ident = ident[:-4]
    return _ARXIV_VERSION.sub("", ident).lower()


def _arxiv_id_from_work(work: dict) -> str:
    locations = [work.get("primary_location") or {}]
    locations.extend(loc or {} for loc in (work.get("locations") or []))
    for loc in locations:
        for url_key in ("landing_page_url", "pdf_url"):
            ident = _arxiv_id_from_url(loc.get(url_key))
            if ident:
                return ident
    return ""


def _pmid_from_work(work: dict) -> str:
    raw = str((work.get("ids") or {}).get("pmid") or "").strip()
    pmid = raw.rstrip("/").rsplit("/", 1)[-1]
    return pmid if pmid.isdigit() else ""
MAX_ATTEMPTS = 5

# Daily-mode page cap per query (ADR-0030). The daily window is
# from_publication_date = today-14d with no upper bound; on 2026-09-14 that
# window held ~3.7k works for the concept query and ~3.9k for the keyword
# query, while the old cap (4 pages x 100) returned the same top-400 every
# day. 60 pages x 200 covers the whole window with headroom; the fetcher
# reports `truncated` in `stats` whenever a query still had a next_cursor.
#
# Cost model (help.openalex.org/access/example-costs, 2026-09): OpenAlex
# bills per API call, not per result. A `search=` call costs $1 per 1,000,
# a filter-only call $0.10 per 1,000; anonymous callers get $0.10/day, a
# free API key $1/day. PER_PAGE=200 (the API maximum) therefore halves the
# spend of every window compared with the old 100.
DAILY_MAX_PAGES = 60
DAILY_SORT = "publication_date:desc"
PER_PAGE = 200
# Historical (backfill) page cap per query and month window. The concept
# query alone returns ~4.2k works/month for every year 2018-2025 (live
# meta.count on 2026-09-14), so the old 40-page cap truncated each month.
HISTORICAL_MAX_PAGES = 60


class OpenAlexError(RuntimeError):
    """Base class for errors that callers may surface in run manifests."""

    code = "openalex_error"


class OpenAlexRateLimitError(OpenAlexError):
    code = "rate_limited"


def _api_key() -> str | None:
    """Return the optional free-account key, if one is configured."""
    return os.environ.get("OPENALEX_API_KEY", "").strip() or None


def _request_json(params: dict) -> dict:
    """GET one page, retrying only transient failures.

    Anonymous basic/search requests remain supported. When a free-account
    key is configured it is added to the request for a larger, account-bound
    allowance. The key is never copied into exception messages or logs.
    Long rate-limit windows fail fast so a daily run does not sit idle.
    """
    safe_params = dict(params)
    key = _api_key()
    if key:
        safe_params["api_key"] = key
    for attempt in range(MAX_ATTEMPTS):
        try:
            response = requests.get(
                OPENALEX_BASE, params=safe_params, timeout=30
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == MAX_ATTEMPTS - 1:
                raise OpenAlexError(
                    f"request failed after {MAX_ATTEMPTS} attempts: "
                    f"{type(exc).__name__}"
                ) from exc
            time.sleep(2 ** (attempt + 1))
            continue

        status_code = getattr(response, "status_code", 200)
        headers = getattr(response, "headers", {})
        if status_code == 429:
            retry_after_raw = headers.get("Retry-After", "0")
            try:
                retry_after = max(0, int(float(retry_after_raw)))
            except (TypeError, ValueError):
                retry_after = 0
            if retry_after > 60 or attempt == MAX_ATTEMPTS - 1:
                raise OpenAlexRateLimitError(
                    f"OpenAlex rate limited the request (retry_after="
                    f"{retry_after}s)"
                )
            time.sleep(retry_after or 2 ** (attempt + 1))
            continue

        if 500 <= status_code < 600:
            if attempt == MAX_ATTEMPTS - 1:
                raise OpenAlexError(
                    f"OpenAlex HTTP {status_code} after "
                    f"{MAX_ATTEMPTS} attempts"
                )
            time.sleep(2 ** (attempt + 1))
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise OpenAlexError(
                f"OpenAlex HTTP {status_code}"
            ) from exc
        return response.json()

    raise OpenAlexError("OpenAlex request exhausted retries")


def _source_id(source: dict | None) -> str:
    """The bare OpenAlex source id (``S12345``) of a work's venue."""
    raw = str((source or {}).get("id") or "").strip()
    ident = raw.rsplit("/", 1)[-1]
    return ident if ident.startswith("S") and ident[1:].isdigit() else ""


def _abstract_from_inverted_index(inv: dict | None) -> str:
    """OpenAlex returns abstracts as an inverted index. Reconstruct text."""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _build_filter(concepts: list[str], from_date: str,
                  to_date: str | None = None) -> str:
    parts = [f"from_publication_date:{from_date}", "type:article|review"]
    if to_date:
        parts.append(f"to_publication_date:{to_date}")
    if concepts:
        parts.append("concepts.id:" + "|".join(concepts))
    return ",".join(parts)


def _fetch_one(flt: str, search_query: str | None, per_page: int,
               max_pages: int, sort: str | None = None,
               stats: dict | None = None) -> list[dict]:
    """One cursor-paginated OpenAlex query → list of normalised paper dicts.

    Extracted so the AND→OR dispatch in :func:`fetch` can issue two
    independent queries and union their results without duplicating the
    pagination loop.
    """
    results: list[dict] = []
    cursor = "*"
    pages = 0
    for _ in range(max_pages):
        params = {
            "filter": flt,
            "per-page": per_page,
            "cursor": cursor,
        }
        email = os.environ.get("OPENALEX_EMAIL", "").strip()
        if email:
            params["mailto"] = email
        if search_query:
            params["search"] = search_query
        if sort:
            params["sort"] = sort

        data = _request_json(params)
        pages += 1

        for work in data.get("results", []):
            results.append(_normalize(work))

        cursor = data.get("meta", {}).get("next_cursor")
        if not cursor:
            break

    if stats is not None:
        # A live next_cursor after the last allowed page means OpenAlex had
        # more matches than we read: the window was truncated (ADR-0030).
        stats["pages"] = pages
        stats["results"] = len(results)
        stats["truncated"] = bool(cursor)
    return results


def fetch(
    concepts: list[str],
    keywords: list[str],
    days_back: int = 1,
    per_page: int = PER_PAGE,
    max_pages: int | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """Returns a list of normalized paper dicts.

    Two modes:
      - Daily (default): from_date computed from `days_back`, no upper
        bound, results sorted newest-first (`DAILY_SORT`). Default
        `max_pages` is `DAILY_MAX_PAGES` per query (ADR-0030).
      - Historical (when both from_date and to_date are set as 'YYYY-MM-DD'):
        both bounds passed to the OpenAlex filter. Default `max_pages` is
        `HISTORICAL_MAX_PAGES` (up to ~12,000 papers/month per query).

    Dispatch — changed from AND to OR semantics on 2026-05-19 after the
    DOI verifier surfaced 4 must_read papers that were concept-relevant
    but slipped past the narrow ``concepts.id`` clause when combined with
    ``search=``:

      - concepts AND keywords (the normal direction config): issue TWO
        queries — query A = concept-filter only, query B = search-only
        on the OR-joined keyword string — then union the result lists and
        deduplicate by OpenAlex paper id. Result counts may roughly
        double for directions that supply both signals.
      - concepts only OR keywords only: the original single-query path
        (preserves behaviour for direction configs that intentionally
        provide just one signal).

    Cursor pagination, per_page, polite mailto, daily/historical mode
    selection, and `max_pages` semantics are unchanged — each query in
    the fan-out is capped at `effective_max_pages` independently.
    Callers may always override `max_pages` explicitly.

    `stats`, when given, is filled with per-query page/result counts and a
    top-level `truncated` flag so the caller can surface a capped window.
    """
    historical = bool(from_date and to_date)
    if historical:
        effective_from = from_date
        effective_to = to_date
        effective_max_pages = max_pages if max_pages is not None else HISTORICAL_MAX_PAGES
        sort = None
    else:
        effective_from = (dt.date.today() - dt.timedelta(days=days_back)).isoformat()
        effective_to = None
        effective_max_pages = max_pages if max_pages is not None else DAILY_MAX_PAGES
        sort = DAILY_SORT

    query_stats: list[dict] = []

    def _run(name: str, flt: str, search: str | None) -> list[dict]:
        one: dict = {"query": name}
        rows = _fetch_one(flt, search, per_page, effective_max_pages,
                          sort=sort, stats=one)
        query_stats.append(one)
        return rows

    def _publish_stats() -> None:
        if stats is None:
            return
        stats["queries"] = query_stats
        stats["max_pages"] = effective_max_pages
        stats["truncated"] = any(q.get("truncated") for q in query_stats)

    has_concepts = bool(concepts)
    has_keywords = bool(keywords)

    if has_concepts and has_keywords:
        # Two-query fan-out: recover papers that match concepts XOR keywords.
        flt_concepts = _build_filter(concepts, effective_from, effective_to)
        flt_no_concepts = _build_filter([], effective_from, effective_to)
        search_query = " OR ".join(f'"{k}"' for k in keywords)
        rows_a = _run("concepts", flt_concepts, None)
        rows_b = _run("search", flt_no_concepts, search_query)
        # Union with stable A-first ordering, dedup by OpenAlex work id.
        seen: set[str] = set()
        merged: list[dict] = []
        for r in rows_a + rows_b:
            rid = r.get("id") or ""
            if rid and rid in seen:
                continue
            if rid:
                seen.add(rid)
            merged.append(r)
        _publish_stats()
        return merged

    # Single-query path (keywords-only or concepts-only) — unchanged.
    flt = _build_filter(concepts, effective_from, effective_to)
    search_query = " OR ".join(f'"{k}"' for k in keywords) if has_keywords else None
    rows = _run("single", flt, search_query)
    _publish_stats()
    return rows


def _normalize(work: dict) -> dict:
    doi = work.get("doi") or ""
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]

    # Extract author names + first author affiliation + corresponding author info
    authorships = work.get("authorships", [])
    authors = [a.get("author", {}).get("display_name", "") for a in authorships]

    def _aff(a: dict) -> str:
        """Best-effort affiliation string: prefer raw, fallback to first institution."""
        raw = a.get("raw_affiliation_strings") or []
        if raw:
            return raw[0]
        insts = a.get("institutions") or []
        if insts:
            return insts[0].get("display_name", "")
        return ""

    first_author_affiliation = _aff(authorships[0]) if authorships else ""

    # Corresponding authors (may be multiple, may be the first author)
    corresponding = [
        {
            "name": a.get("author", {}).get("display_name", ""),
            "affiliation": _aff(a),
        }
        for a in authorships if a.get("is_corresponding")
    ]

    concepts = [
        c.get("display_name", "")
        for c in work.get("concepts", [])
        if c.get("score", 0) > 0.3
    ]

    venue = (work.get("primary_location") or {}).get("source") or {}

    # ADR-0015 §4.1: OpenAlex always emits publication_date as YYYY-MM-DD,
    # even when the source record is month-only or year-only. Infer precision
    # heuristically from the trailing suffix.
    raw_date = work.get("publication_date", "") or ""
    if not raw_date:
        date_str, date_precision = "", "year"
    elif raw_date.endswith("-01-01"):
        date_str, date_precision = raw_date, "year"
    elif raw_date.endswith("-01"):
        date_str, date_precision = raw_date, "month"
    else:
        date_str, date_precision = raw_date, "day"

    return {
        "source": "openalex",
        "id": work.get("id", ""),
        "doi": doi,
        # ADR-0031: exact secondary identifiers for canonical dedup.
        "pmid": _pmid_from_work(work),
        "arxiv_id": _arxiv_id_from_work(work),
        "title": work.get("title", "") or "",
        "abstract": _abstract_from_inverted_index(work.get("abstract_inverted_index")),
        "authors": authors,
        "first_author_affiliation": first_author_affiliation,
        "corresponding_authors": corresponding,
        "venue": venue.get("display_name", ""),
        # ADR-0035: the display name alone cannot be queried back. The source
        # id is what "what else did this journal publish this month" needs,
        # and `venue_type` separates a real journal from a preprint server.
        "venue_id": _source_id(venue),
        "venue_issn_l": venue.get("issn_l") or "",
        "venue_type": venue.get("type", "") or "",
        "year": work.get("publication_year"),
        "date": date_str,
        "date_precision": date_precision,
        "url": work.get("doi") or work.get("id", ""),
        "cited_by_count": work.get("cited_by_count", 0),
        "concepts": concepts,
        "categories": [],
        "raw_type": work.get("type", ""),
    }


# --- ADR-0035: uniform random sampling inside one journal-month ------------
#
# Reading a whole month of a megajournal would be ~15 pages of results to
# throw almost all of away. OpenAlex's basic `page` paging addresses a single
# work directly, so one call establishes the size of the pool and one call
# per pick draws from it — a genuinely uniform sample for ~3 filter calls
# ($0.10 per 1,000) instead of fifteen.
#
# Basic paging cannot reach past 10,000 results; a journal-month that large
# is sampled from its first 10,000, which the caller is told about.
PAGE_LIMIT = 10_000


# Papers stored before ADR-0035 have no venue_id, so a backfill has to ask
# OpenAlex what journal they came from. Batched: 50 works per call, and
# `select` keeps the payload to the one field that matters.
RESOLVE_BATCH = 50


def resolve_sources(work_ids: list[str]) -> dict[str, dict]:
    """Map bare OpenAlex work ids to their journal, in batches.

    Unknown or unresolvable ids are simply absent from the result; a caller
    backfilling old records must cope with that anyway.
    """
    wanted = [w for w in dict.fromkeys(work_ids) if w]
    out: dict[str, dict] = {}
    for start in range(0, len(wanted), RESOLVE_BATCH):
        chunk = wanted[start:start + RESOLVE_BATCH]
        data = _request_json({
            "filter": "openalex_id:" + "|".join(chunk),
            "per-page": RESOLVE_BATCH,
            "select": "id,primary_location",
        })
        for work in data.get("results", []):
            ident = str(work.get("id") or "").rsplit("/", 1)[-1]
            source = (work.get("primary_location") or {}).get("source") or {}
            out[ident] = {
                "venue_id": _source_id(source),
                "venue": source.get("display_name", "") or "",
                "venue_issn_l": source.get("issn_l") or "",
                "venue_type": source.get("type", "") or "",
            }
    return out


def journal_month_filter(source_id: str, from_date: str, to_date: str) -> str:
    return (f"primary_location.source.id:{source_id},"
            f"from_publication_date:{from_date},"
            f"to_publication_date:{to_date},type:article|review")


def journal_month_count(source_id: str, from_date: str, to_date: str) -> int:
    """How many articles/reviews this journal published in the window."""
    data = _request_json({
        "filter": journal_month_filter(source_id, from_date, to_date),
        "per-page": 1,
    })
    return int((data.get("meta") or {}).get("count") or 0)


def journal_work_at(source_id: str, from_date: str, to_date: str,
                    position: int) -> dict | None:
    """The work at a 1-based position in the journal-month listing."""
    if position < 1 or position > PAGE_LIMIT:
        return None
    data = _request_json({
        "filter": journal_month_filter(source_id, from_date, to_date),
        "per-page": 1,
        "page": position,
    })
    results = data.get("results") or []
    return _normalize(results[0]) if results else None


if __name__ == "__main__":
    sample = fetch(
        concepts=[],
        keywords=["bioprinting", "bioink"],
        days_back=7,
    )
    print(f"Fetched {len(sample)} papers")
    for p in sample[:3]:
        print(f"  - {p['title'][:80]}  [{p['doi']}]")
