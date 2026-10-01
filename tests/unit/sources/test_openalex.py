"""OpenAlex fetcher: date precision, concept/keyword fan-out, auth + retry,
newest-first daily paging and truncation stats (ported from four v1 files)."""
from __future__ import annotations

import pytest

from radar.sources import openalex as oa


class _Resp:
    def __init__(self, results=None, status=200, headers=None):
        self._results = results or []
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise oa.requests.HTTPError(str(self.status_code))

    def json(self):
        return {"results": self._results, "meta": {"next_cursor": None}}


def _bare_work(publication_date, wid="W1", doi="https://doi.org/10.1/test") -> dict:
    w = {"id": f"https://openalex.org/{wid}", "doi": doi, "title": "test paper",
         "publication_year": 2024, "type": "article"}
    if publication_date is not None:
        w["publication_date"] = publication_date
    return w


def _install_single(monkeypatch, work):
    monkeypatch.setattr(oa.requests, "get", lambda url, params=None, **kw: _Resp([work]))


# --- date precision --------------------------------------------------------

@pytest.mark.parametrize("date, precision", [
    ("2024-03-15", "day"), ("2024-03-01", "month"), ("2024-01-01", "year"),
    ("2024-12-01", "month"), (None, "year"), ("", "year"),
])
def test_date_precision_heuristic(monkeypatch, date, precision):
    _install_single(monkeypatch, _bare_work(date))
    [p] = oa.fetch(concepts=[], keywords=["x"], from_date="2024-01-01", to_date="2024-12-31")
    assert p["date"] == (date or "")
    assert p["date_precision"] == precision


# --- normaliser ------------------------------------------------------------

def test_normalizer_exposes_arxiv_id_and_pmid():
    work = {
        "id": "https://openalex.org/W7207880362", "doi": None, "title": "t",
        "publication_year": 2026, "publication_date": "2026-09-02", "type": "preprint",
        "ids": {"openalex": "https://openalex.org/W7207880362",
                "pmid": "https://pubmed.ncbi.nlm.nih.gov/42709028"},
        "primary_location": {"landing_page_url": "https://arxiv.org/abs/2609.00965",
                             "pdf_url": "https://arxiv.org/pdf/2609.00965",
                             "source": {"display_name": "arXiv (Cornell University)"}},
        "locations": [],
    }
    paper = oa._normalize(work)
    assert paper["arxiv_id"] == "2609.00965"
    assert paper["pmid"] == "42709028"
    assert paper["doi"] == ""


def test_normalizer_without_ids_or_locations():
    paper = oa._normalize({"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/x",
                           "title": "t", "publication_year": 2026, "publication_date": "2026-09-02"})
    assert paper["arxiv_id"] == "" and paper["pmid"] == ""
    assert paper["doi"] == "10.1/x"


# --- fan-out ---------------------------------------------------------------

def test_concepts_and_keywords_fan_out_into_two_queries_and_union(monkeypatch):
    calls: list[dict] = []
    a = _bare_work("2024-05-15", "W_AAA", "https://doi.org/10.1/concept-hit")
    b = _bare_work("2024-05-10", "W_BBB", "https://doi.org/10.1038/s41598-024-61305-x")
    both = _bare_work("2024-05-20", "W_OVR", "https://doi.org/10.1/overlap")

    def fake_get(url, params=None, **kw):
        calls.append(dict(params or {}))
        if "search" in (params or {}):
            return _Resp([b, both])
        if "concepts.id:" in (params or {}).get("filter", ""):
            return _Resp([a, both])
        return _Resp([])

    monkeypatch.setattr(oa.requests, "get", fake_get)
    out = oa.fetch(concepts=["C3020736514", "C3019025420"], keywords=["femoral stem", "stress shielding"],
                   from_date="2024-05-01", to_date="2024-05-31", max_pages=1)
    assert len(calls) == 2
    concept_call = next(c for c in calls if "search" not in c)
    search_call = next(c for c in calls if "search" in c)
    assert "concepts.id:" in concept_call["filter"]
    assert "concepts.id:" not in search_call["filter"]
    assert "femoral stem" in search_call["search"]
    dois = sorted(p["doi"] for p in out)
    assert dois == ["10.1/concept-hit", "10.1/overlap", "10.1038/s41598-024-61305-x"]


def test_single_signal_keeps_single_query(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(oa.requests, "get", lambda url, params=None, **kw: calls.append(dict(params or {})) or _Resp())
    oa.fetch(concepts=["C123"], keywords=[], from_date="2024-05-01", to_date="2024-05-31", max_pages=1)
    oa.fetch(concepts=[], keywords=["x"], from_date="2024-05-01", to_date="2024-05-31", max_pages=1)
    assert len(calls) == 2


# --- auth and retry --------------------------------------------------------

def test_missing_api_key_uses_anonymous_request(monkeypatch):
    calls = []
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    monkeypatch.setattr(oa.requests, "get", lambda url, params, timeout: calls.append(params) or _Resp())
    assert oa.fetch([], ["x"], max_pages=1) == []
    assert "api_key" not in calls[0]
    assert calls[0]["per-page"] == oa.PER_PAGE


def test_api_key_is_injected(monkeypatch):
    calls = []
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-test-key")
    monkeypatch.setattr(oa.requests, "get", lambda url, params, timeout: calls.append(params) or _Resp())
    oa.fetch([], ["x"], max_pages=1)
    assert calls[0]["api_key"] == "secret-test-key"


def test_503_retries_then_succeeds(monkeypatch):
    responses = iter([_Resp(status=503), _Resp(status=503), _Resp()])
    sleeps = []
    monkeypatch.setattr(oa.requests, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(oa.time, "sleep", sleeps.append)
    assert oa.fetch([], ["x"], max_pages=1) == []
    assert sleeps == [2, 4]


def test_long_rate_limit_fails_fast_without_leaking_key(monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "never-print-this")
    monkeypatch.setattr(oa.requests, "get", lambda *a, **kw: _Resp(status=429, headers={"Retry-After": "120"}))
    with pytest.raises(oa.OpenAlexRateLimitError) as caught:
        oa.fetch([], ["x"], max_pages=1)
    assert "never-print-this" not in str(caught.value)


# --- daily paging and truncation -------------------------------------------

def _install_pages(monkeypatch, pages_available: int, calls: list):
    counter = {"n": 0}

    class PagedResp:
        def raise_for_status(self): pass

        def json(self):
            counter["n"] += 1
            work = _bare_work("2026-09-01", f"W{counter['n']}", f"https://doi.org/10.1/w{counter['n']}")
            more = counter["n"] < pages_available
            return {"results": [work], "meta": {"next_cursor": "next" if more else None}}

    monkeypatch.setattr(oa.requests, "get", lambda url, params=None, **kw: calls.append(dict(params or {})) or PagedResp())


def test_daily_sorts_newest_first_and_uses_daily_cap(monkeypatch):
    calls: list[dict] = []
    _install_pages(monkeypatch, 1, calls)
    stats: dict = {}
    oa.fetch(concepts=[], keywords=["x"], days_back=14, stats=stats)
    assert calls[0]["sort"] == oa.DAILY_SORT
    assert stats["max_pages"] == oa.DAILY_MAX_PAGES
    assert oa.DAILY_MAX_PAGES >= 40


def test_historical_mode_does_not_sort(monkeypatch):
    calls: list[dict] = []
    _install_pages(monkeypatch, 1, calls)
    oa.fetch(concepts=[], keywords=["x"], from_date="2024-01-01", to_date="2024-01-31")
    assert "sort" not in calls[0]


def test_truncation_reported_when_cursor_outlives_cap(monkeypatch):
    _install_pages(monkeypatch, 10, [])
    stats: dict = {}
    out = oa.fetch(concepts=[], keywords=["x"], days_back=14, max_pages=3, stats=stats)
    assert len(out) == 3
    assert stats["truncated"] is True
    assert stats["queries"][0] == {"query": "single", "pages": 3, "results": 3, "truncated": True}


def test_no_truncation_when_window_exhausted(monkeypatch):
    _install_pages(monkeypatch, 2, [])
    stats: dict = {}
    out = oa.fetch(concepts=[], keywords=["x"], days_back=14, max_pages=5, stats=stats)
    assert len(out) == 2
    assert stats["truncated"] is False


def test_fan_out_reports_both_queries(monkeypatch):
    _install_pages(monkeypatch, 1, [])
    stats: dict = {}
    oa.fetch(concepts=["C1"], keywords=["x"], days_back=14, stats=stats)
    assert [q["query"] for q in stats["queries"]] == ["concepts", "search"]
