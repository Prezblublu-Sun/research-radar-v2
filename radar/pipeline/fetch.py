"""Stage 1: ask each source for its window. One source failing is a warning.

Daily mode uses per-source lookback floors, ported from v1 (ADR-0030):
arXiv filters on the v1 submission date but a paper only becomes visible
after its announcement, which lags by 1-4 days, so a one-day window
returned zero on weekends; OpenAlex indexes publisher deposits days to
weeks late, so its window stays wide and relies on dedup for repeats.

Historical mode (``ctx.window_from``/``window_to``, used by the backfill)
asks every source for one inclusive date range instead.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ARXIV_MIN_LOOKBACK_DAYS = 5
OPENALEX_MIN_LOOKBACK_DAYS = 14


@dataclass
class FetchResult:
    lists: list[list[dict]] = field(default_factory=list)
    source_status: dict[str, dict] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)
    openalex_stats: dict = field(default_factory=dict)
    window: dict = field(default_factory=dict)
    sources_used: dict = field(default_factory=dict)

    @property
    def fetched_total(self) -> int:
        return sum(len(lst) for lst in self.lists)

    @property
    def errors(self) -> list[str]:
        return [name for name, state in self.source_status.items() if state.get("status") == "error"]

    def error_code(self, name: str) -> str:
        return str((self.source_status.get(name) or {}).get("error_code") or "")


def _record(result: FetchResult, name: str, papers: list[dict] | None = None,
            error: Exception | None = None, log=print) -> None:
    if error is not None:
        result.source_status[name] = {"status": "error", "count": 0,
                                      "error_code": type(error).__name__, "message": str(error)[:300]}
        log(f"  ! {name} failed: {type(error).__name__}: {error}")
        return
    papers = papers or []
    result.lists.append(papers)
    result.source_counts[name] = len(papers)
    result.source_status[name] = {"status": "ok" if papers else "empty", "count": len(papers),
                                  "error_code": None, "message": ""}
    log(f"  -> {name}: {len(papers)} papers")


def fetch_stage(ctx, log=print) -> FetchResult:
    cfg = ctx.config
    result = FetchResult()
    if ctx.historical:
        span = {"from_date": ctx.window_from, "to_date": ctx.window_to}
        result.window = {"mode": "historical", "from": ctx.window_from, "to": ctx.window_to}
        arxiv_kwargs = openalex_kwargs = pubmed_kwargs = span
        describe = f"{ctx.window_from}..{ctx.window_to}"
    else:
        arxiv_days = max(ctx.days_back, ARXIV_MIN_LOOKBACK_DAYS)
        openalex_days = max(ctx.days_back, OPENALEX_MIN_LOOKBACK_DAYS)
        result.window = {"mode": "daily", "requested_days_back": ctx.days_back,
                         "arxiv": arxiv_days, "openalex": openalex_days, "pubmed": ctx.days_back}
        arxiv_kwargs, openalex_kwargs, pubmed_kwargs = (
            {"days_back": arxiv_days}, {"days_back": openalex_days}, {"days_back": ctx.days_back})
        describe = "lookback"
    result.sources_used = {
        "arxiv": {"categories": cfg.arxiv_categories, **arxiv_kwargs},
        "openalex": {"keywords": cfg.openalex_keywords, "concepts": cfg.openalex_concepts, **openalex_kwargs},
        "pubmed": {"terms": cfg.pubmed_terms, **pubmed_kwargs},
    }

    if "arxiv" in ctx.sources and cfg.arxiv_categories:
        log(f"Fetching arXiv ({describe} {arxiv_kwargs}): {len(cfg.arxiv_categories)} categories")
        try:
            _record(result, "arxiv", ctx.fetchers.arxiv.fetch(cfg.arxiv_categories, **arxiv_kwargs), log=log)
        except Exception as error:  # noqa: BLE001 — a source outage is a warning
            _record(result, "arxiv", error=error, log=log)

    if "openalex" in ctx.sources and (cfg.openalex_keywords or cfg.openalex_concepts):
        log(f"Fetching OpenAlex ({describe} {openalex_kwargs}): "
            f"{len(cfg.openalex_keywords)} keywords, {len(cfg.openalex_concepts)} concepts")
        try:
            stats: dict = {}
            papers = ctx.fetchers.openalex.fetch(concepts=cfg.openalex_concepts, keywords=cfg.openalex_keywords,
                                                 stats=stats, **openalex_kwargs)
            _record(result, "openalex", papers, log=log)
            result.openalex_stats = dict(stats)
            if stats:
                result.source_status["openalex"]["fetch_stats"] = dict(stats)
            if stats.get("truncated"):
                log("  ! OpenAlex window TRUNCATED at the page cap")
        except Exception as error:  # noqa: BLE001
            _record(result, "openalex", error=error, log=log)

    if "pubmed" in ctx.sources and cfg.pubmed_terms:
        log(f"Fetching PubMed ({describe} {pubmed_kwargs}): {len(cfg.pubmed_terms)} terms")
        try:
            _record(result, "pubmed", ctx.fetchers.pubmed.fetch(cfg.pubmed_terms, **pubmed_kwargs), log=log)
        except Exception as error:  # noqa: BLE001
            _record(result, "pubmed", error=error, log=log)

    return result
