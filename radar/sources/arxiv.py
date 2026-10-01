"""arXiv fetcher: pulls papers in specified categories submitted in the last N days.
Uses the `arxiv` package which wraps the official arXiv API."""
from __future__ import annotations
import datetime as dt
import time
import arxiv


def fetch(categories: list[str], days_back: int = 1,
          max_results: int | None = None,
          from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Returns a list of normalized paper dicts for papers in the given
    arxiv categories.

    Two modes:
      - Daily (default): submitted within the last `days_back` days, iterating
        results sorted by date descending until older than the cutoff.
        Default `max_results` is 2000 per lookback day (ADR-0030), so a
        wider window is not silently capped at one day's volume.
      - Historical (when both from_date and to_date are set as 'YYYY-MM-DD'):
        uses an arXiv `submittedDate:[YYYYMMDD0000 TO YYYYMMDD2359]` query
        and iterates all results in the window (no cutoff break).
        Default `max_results` is bumped to 10000 internally to cover a
        full calendar month of arxiv traffic.

    Callers may always override `max_results` explicitly.

    Outer retry: if arxiv returns 429 (rate limit), wait 60 / 300 / 900
    seconds between attempts (4 total, ~21 min worst case; the daily job
    allows 90). The arxiv package's internal num_retries only does ~10 second
    waits which arxiv often rejects, and two consecutive daily runs
    (2026-09-13/14) still failed after the old 60 s + 180 s schedule.
    """
    if not categories:
        return []
    cat_query = " OR ".join(f"cat:{c}" for c in categories)

    historical = bool(from_date and to_date)
    if historical:
        date_query = (
            f"submittedDate:[{from_date.replace('-', '')}0000"
            f" TO {to_date.replace('-', '')}2359]"
        )
        query = f"({cat_query}) AND {date_query}"
        cutoff_date = None  # iterate all results in the window
        effective_max_results = max_results if max_results is not None else 10000
    else:
        query = cat_query
        cutoff_date = dt.date.today() - dt.timedelta(days=days_back)
        effective_max_results = (max_results if max_results is not None
                                 else 2000 * max(1, int(days_back)))

    wait_seconds = [0, 60, 300, 900]
    last_error = None

    for attempt, wait in enumerate(wait_seconds, 1):
        if wait > 0:
            print(f"  arxiv attempt {attempt}/{len(wait_seconds)} after {wait}s wait...")
            time.sleep(wait)
        try:
            search = arxiv.Search(
                query=query,
                max_results=effective_max_results,
                sort_by=arxiv.SortCriterion.SubmittedDate,
                sort_order=arxiv.SortOrder.Descending,
            )
            client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
            out: list[dict] = []
            for result in client.results(search):
                if cutoff_date is not None and result.published.date() < cutoff_date:
                    break
                out.append(_normalize(result))
            return out
        except Exception as e:
            last_error = e
            err_str = str(e)
            if "429" in err_str or "HTTPError" in err_str:
                if attempt < len(wait_seconds):
                    print(f"  arxiv hit 429 on attempt {attempt}, will retry...")
                    continue
            else:
                raise

    raise RuntimeError(f"arxiv failed after {len(wait_seconds)} attempts: {last_error}")


def _normalize(r) -> dict:
    """Map arxiv.Result to our unified schema."""
    doi = r.doi or ""
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    return {
        "source": "arxiv",
        "id": r.entry_id,
        "doi": doi,
        "arxiv_id": r.entry_id.rsplit("/", 1)[-1],
        "title": r.title.replace("\n", " ").strip(),
        "abstract": r.summary.replace("\n", " ").strip(),
        "authors": [a.name for a in r.authors],
        "venue": "arXiv preprint",
        "year": r.published.year,
        "date": r.published.date().isoformat(),
        # arxiv's `published` field is the v1 submission date (verified empirically against multi-version papers, ADR-0015 §7(i))
        "date_precision": "day",
        "url": r.entry_id,
        "cited_by_count": 0,
        "concepts": [],
        "categories": [c for c in r.categories],
        "raw_type": "preprint",
    }


if __name__ == "__main__":
    sample = fetch(["cs.LG", "cs.CV"], days_back=1, max_results=20)
    print(f"Fetched {len(sample)} papers")
    for p in sample[:3]:
        print(f"  - {p['title'][:80]}  [{p['arxiv_id']}]")
