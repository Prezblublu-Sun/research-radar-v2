"""arXiv fetcher: date precision and lookback-scaled result caps (ported)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from radar.sources import arxiv as arxiv_fetcher


def _fake_result(published: dt.datetime) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="http://arxiv.org/abs/2401.00001v1", doi="10.1/test",
        title="Test paper", summary="Test abstract",
        authors=[SimpleNamespace(name="A. Author")], categories=["cs.LG"],
        published=published,
    )


def _install(monkeypatch, results, captured: dict | None = None):
    class FakeClient:
        def __init__(self, *a, **kw): pass
        def results(self, search):
            return iter(results)

    def fake_search(**kw):
        if captured is not None:
            captured.update(kw)
        return SimpleNamespace(**kw)

    monkeypatch.setattr(arxiv_fetcher.arxiv, "Client", FakeClient)
    monkeypatch.setattr(arxiv_fetcher.arxiv, "Search", fake_search)


def test_normalized_paper_has_day_precision_and_iso_date(monkeypatch):
    _install(monkeypatch, [_fake_result(dt.datetime(2024, 1, 15, 12, 0, 0))])
    papers = arxiv_fetcher.fetch(["cs.LG"], from_date="2024-01-01", to_date="2024-01-31")
    assert len(papers) == 1
    assert papers[0]["date_precision"] == "day"
    assert papers[0]["date"] == "2024-01-15"
    assert papers[0]["arxiv_id"] == "2401.00001v1"


def test_daily_max_results_scales_with_lookback(monkeypatch):
    captured: dict = {}
    _install(monkeypatch, [], captured)
    arxiv_fetcher.fetch(["cs.LG"], days_back=5)
    assert captured["max_results"] == 10000


def test_one_day_keeps_legacy_cap(monkeypatch):
    captured: dict = {}
    _install(monkeypatch, [], captured)
    arxiv_fetcher.fetch(["cs.LG"], days_back=1)
    assert captured["max_results"] == 2000
