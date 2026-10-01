"""Per-source lookback floors and fail-soft fetching (v1 ADR-0030)."""
from __future__ import annotations

from radar.pipeline import fetch


def test_effective_lookbacks_apply_floors(make_ctx, stub_fetchers, paper_factory):
    fetchers = stub_fetchers(arxiv=[paper_factory("arxiv", arxiv_id="2601.1v1")],
                             openalex=[paper_factory("openalex", doi="10.1/oa", oa_id="https://openalex.org/W1")],
                             pubmed=[paper_factory("pubmed", pmid="1")])
    result = fetch.fetch_stage(make_ctx(fetchers, days_back=2), log=lambda *_: None)
    assert fetchers.calls["arxiv"]["days_back"] == fetch.ARXIV_MIN_LOOKBACK_DAYS
    assert fetchers.calls["openalex"]["days_back"] == fetch.OPENALEX_MIN_LOOKBACK_DAYS
    assert fetchers.calls["pubmed"]["days_back"] == 2
    assert result.window == {"mode": "daily", "requested_days_back": 2, "arxiv": fetch.ARXIV_MIN_LOOKBACK_DAYS,
                             "openalex": fetch.OPENALEX_MIN_LOOKBACK_DAYS, "pubmed": 2}
    assert result.fetched_total == 3
    assert result.source_counts == {"arxiv": 1, "openalex": 1, "pubmed": 1}
    assert result.sources_used["arxiv"]["days_back"] == fetch.ARXIV_MIN_LOOKBACK_DAYS


def test_arxiv_floor_covers_the_announcement_lag():
    assert fetch.ARXIV_MIN_LOOKBACK_DAYS >= 4


def test_one_source_failing_is_recorded_not_raised(make_ctx, stub_fetchers, paper_factory):
    fetchers = stub_fetchers(arxiv=RuntimeError("429 too many requests"),
                             openalex=[paper_factory("openalex", doi="10.1/oa", oa_id="https://openalex.org/W1")],
                             pubmed=[])
    result = fetch.fetch_stage(make_ctx(fetchers), log=lambda *_: None)
    assert result.errors == ["arxiv"]
    assert result.source_status["arxiv"]["status"] == "error"
    assert "429" in result.source_status["arxiv"]["message"]
    assert result.source_status["pubmed"]["status"] == "empty"
    assert result.fetched_total == 1


def test_openalex_truncation_is_surfaced(make_ctx, stub_fetchers, paper_factory):
    fetchers = stub_fetchers(openalex=[paper_factory("openalex", doi="10.1/oa", oa_id="https://openalex.org/W1")],
                             stats={"truncated": True, "max_pages": 60, "queries": []})
    result = fetch.fetch_stage(make_ctx(fetchers, sources=frozenset({"openalex"})), log=lambda *_: None)
    assert result.openalex_stats["truncated"] is True
    assert result.source_status["openalex"]["fetch_stats"]["truncated"] is True
    assert "arxiv" not in result.source_status


def test_source_term_unions_come_from_config(make_ctx, stub_fetchers, cfg):
    fetchers = stub_fetchers(arxiv=[], openalex=[], pubmed=[])
    fetch.fetch_stage(make_ctx(fetchers), log=lambda *_: None)
    assert fetchers.calls["openalex"]["concepts"] == cfg.openalex_concepts
    assert fetchers.calls["openalex"]["keywords"] == cfg.openalex_keywords
    assert cfg.arxiv_categories and cfg.pubmed_terms
