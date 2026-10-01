"""End-to-end daily run offline: fixture fetchers, dry-run / replay scorers,
one append-only file, honest header, empty-run guard."""
from __future__ import annotations

import json

import pytest

from radar.core import identity
from radar.core.atomic import WouldOverwrite
from radar.pipeline import daily
from radar.pipeline.scorer import ReplayScorer
from radar.store import runs
from radar.store.seen import SeenKeys


def _by_source(records):
    out = {"arxiv": [], "openalex": [], "pubmed": []}
    for r in records:
        out[r["source"]].append(r)
    return out


def test_offline_dry_run_writes_header_first_and_ck_first(make_ctx, stub_fetchers, sample_records, data_root, cfg):
    ctx = make_ctx(stub_fetchers(**_by_source(sample_records)), dry_run=True)
    report = daily.run_daily(ctx, log=lambda *_: None)

    assert report.ok, report.blocking
    path = report.run_path
    assert path == data_root.run_file(ctx.run_id, "daily")
    header = runs.read_header(path)
    assert header["kind"] == "run" and header["run_id"] == ctx.run_id
    assert header["run_status"] == "success"
    assert header["config"]["directions_yaml"] == cfg.sha
    assert header["config"]["scorer_prompt_file"] == "scorer_v4.txt"
    assert header["counts"]["fetched"] == len(sample_records)
    assert header["counts"]["after_routing"] == header["counts"]["records_written"] == len(report.papers)
    assert header["counts"]["after_routing"] > 0

    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[1:]:
        assert line.startswith('{"ck":"'), line[:60]
        record = json.loads(line)
        assert record["kind"] == "paper"
        assert record["ck"] == identity.canonical_key(record)
        assert record["direction"] in cfg.keys
        assert record["llm"] == {"priority": None, "dry_run": True}
        assert "concepts" not in record
        assert len(record["abstract"]) <= 3000
        assert record["run_id"] == ctx.run_id and record["prompt_sha"] == ctx.prompt.sha
    # The regex scan must agree with a full parse — that is why ck is first.
    assert runs.scan_keys(path) == {json.loads(l)["ck"] for l in lines[1:]}


def test_dry_run_never_touches_the_seen_cache(make_ctx, stub_fetchers, sample_records, data_root):
    ctx = make_ctx(stub_fetchers(**_by_source(sample_records)), dry_run=True)
    daily.run_daily(ctx, log=lambda *_: None)
    assert not (data_root.cache / "seen_keys.json").exists()


def test_second_run_dedups_against_the_first_and_refuses_overwrite(make_ctx, stub_fetchers, sample_records, data_root):
    fetchers = stub_fetchers(**_by_source(sample_records))
    first = daily.run_daily(make_ctx(fetchers), log=lambda *_: None)
    assert (data_root.cache / "seen_keys.json").exists()
    assert len(SeenKeys.load(data_root)) == first.header["counts"]["after_dedup"]

    second = daily.run_daily(make_ctx(fetchers, run_id="2026-10-02T120000Z"), log=lambda *_: None)
    assert second.header["counts"]["already_seen"] == first.header["counts"]["after_dedup"]
    assert second.header["counts"]["after_dedup"] == 0
    assert second.run_path is not None  # a run with fetched papers but nothing new is still a run
    assert runs.read_header(second.run_path)["counts"]["records_written"] == 0

    with pytest.raises(WouldOverwrite):
        daily.run_daily(make_ctx(fetchers), log=lambda *_: None)


def test_empty_fetch_writes_nothing_and_blocks(make_ctx, stub_fetchers, data_root):
    report = daily.run_daily(make_ctx(stub_fetchers(arxiv=[], openalex=[], pubmed=[])), log=lambda *_: None)
    assert report.run_path is None
    assert not report.ok
    assert report.header["run_status"] == "failed"
    assert "fetched_zero" in report.header["quality_flags"]
    assert runs.list_runs(data_root) == []


def test_one_source_down_is_partial_success_and_still_published(make_ctx, stub_fetchers, sample_records):
    lists = _by_source(sample_records)
    lists["arxiv"] = RuntimeError("arXiv 429")
    report = daily.run_daily(make_ctx(stub_fetchers(**lists)), log=lambda *_: None)
    assert report.ok
    assert report.header["run_status"] == "partial_success"
    assert "arxiv_failed" in report.header["quality_flags"]
    assert report.header["source_status"]["arxiv"]["status"] == "error"


def test_truncation_flag_and_replay_scoring_with_crossover_boost(make_ctx, stub_fetchers, sample_records, cfg):
    lists = _by_source(sample_records)
    fetchers = stub_fetchers(**lists, stats={"truncated": True, "max_pages": 60, "queries": []})
    answers = {r.get("doi") or "": {"priority": "Medium"} for r in sample_records}
    ctx = make_ctx(fetchers, scorer=ReplayScorer(answers))
    report = daily.run_daily(ctx, log=lambda *_: None)
    assert "openalex_truncated" in report.header["quality_flags"]
    assert report.header["source_status"]["openalex"]["fetch_stats"]["truncated"] is True
    counts = report.header["counts"]["priority_counts"]
    assert counts["Medium"] + counts["High"] == len([p for p in report.papers if not p["llm"].get("scorer_failed")])
    boosted = [p for p in report.papers if p["llm"].get("priority_boosted")]
    assert report.header["counts"]["boosted"] == len(boosted)
    for p in boosted:
        assert p["llm"]["priority"] == "High" and p["llm"]["priority_pre_boost"] == "Medium"
        assert set(p["directions"]) >= set(p["crossover"][0]) if p.get("crossover") else True
    assert report.header["config"]["scorer_version"] == "replay"


class _JournalStub:
    """An openalex fetcher that also answers the random-reading journal calls."""
    PAGE_LIMIT = 10_000

    def __init__(self, inner, counts):
        self.inner, self.counts, self.calls = inner, counts, []

    def fetch(self, *args, **kwargs):
        return self.inner.fetch(*args, **kwargs)

    def journal_month_count(self, source_id, from_date, to_date):
        self.calls.append(("count", source_id))
        return self.counts.get(source_id, 0)

    def journal_work_at(self, source_id, from_date, to_date, position):
        self.calls.append(("draw", source_id, position))
        return {"source": "openalex", "id": f"https://openalex.org/WR{position}",
                "doi": f"10.9/random-{source_id}-{position}", "title": f"Random work {position}",
                "abstract": "", "authors": [], "venue": "J", "venue_id": source_id,
                "venue_type": "journal", "date": from_date, "year": 2026}


def _with_journals(fetchers, counts):
    from radar.pipeline.context import Fetchers
    return Fetchers(arxiv=fetchers.arxiv, openalex=_JournalStub(fetchers.openalex, counts), pubmed=fetchers.pubmed)


def test_random_stage_draws_from_high_journals_into_its_own_stream(make_ctx, stub_fetchers, sample_records, data_root, cfg):
    journals = {r["venue_id"] for r in sample_records if r.get("venue_type") == "journal" and r.get("venue_id")}
    assert journals, "fixture needs at least one journal record"
    fetchers = _with_journals(stub_fetchers(**_by_source(sample_records)), {j: 40 for j in journals})
    ctx = make_ctx(fetchers, offline=False, random_reading=True,
                   scorer=ReplayScorer({}, default={"priority": "High"}))
    report = daily.run_daily(ctx, log=lambda *_: None)

    assert report.random_path == data_root.random_reading / "2026" / f"{ctx.run_id}.jsonl"
    summary = report.header["random_reading"]
    assert summary["status"] == "ok" and summary["journals"] >= 1 and summary["papers"] == len(report.random_papers) > 0
    lines = report.random_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "random_reading"
    random_keys = runs.scan_keys(report.random_path)
    assert random_keys and random_keys.isdisjoint(runs.scan_keys(report.run_path))
    # Isolation: random picks never enter the corpus seen set or the day's counts.
    assert random_keys.isdisjoint(SeenKeys.load(data_root).keys)
    assert sum(report.header["counts"]["priority_counts"].values()) == len(report.papers)
    for paper in report.random_papers:
        assert paper["llm"]["priority"] == "High" and paper["random_reading"]["venue_id"] in journals


def test_random_stage_is_skipped_offline_and_when_disabled(make_ctx, stub_fetchers, sample_records, data_root):
    fetchers = stub_fetchers(**_by_source(sample_records))
    report = daily.run_daily(make_ctx(fetchers, offline=True, random_reading=True), log=lambda *_: None)
    assert report.header["random_reading"] == {"status": "skipped_offline"} and report.random_path is None
    report = daily.run_daily(make_ctx(fetchers, run_id="2026-10-02T120000Z", offline=False, random_reading=False),
                             log=lambda *_: None)
    assert report.header["random_reading"] == {"status": "disabled"}
    assert not data_root.random_reading.exists()


def test_random_stage_failure_never_fails_the_run(make_ctx, stub_fetchers, sample_records, monkeypatch):
    from radar.pipeline import random_reading as rr

    def boom(*a, **kw):
        raise RuntimeError("serendipity exploded")

    monkeypatch.setattr(rr, "collect", boom)
    fetchers = stub_fetchers(**_by_source(sample_records))
    report = daily.run_daily(make_ctx(fetchers, offline=False, random_reading=True,
                                      scorer=ReplayScorer({}, default={"priority": "High"})), log=lambda *_: None)
    assert report.ok and report.run_path is not None
    assert report.header["random_reading"]["status"] == "failed"
    assert "serendipity exploded" in report.header["random_reading"]["error"]


def test_alias_resolver_failure_does_not_stop_the_run(make_ctx, stub_fetchers, sample_records, monkeypatch):
    from radar.sources import zenodo_aliases

    def boom(*a, **kw):
        raise RuntimeError("zenodo exploded")

    monkeypatch.setattr(zenodo_aliases, "resolve_zenodo", boom)
    report = daily.run_daily(make_ctx(stub_fetchers(**_by_source(sample_records)), offline=False),
                             log=lambda *_: None)
    assert report.ok and report.run_path is not None


def test_offline_run_makes_no_alias_lookups(make_ctx, stub_fetchers, sample_records, monkeypatch):
    from radar.sources import zenodo_aliases

    def boom(*a, **kw):
        raise AssertionError("network call in offline mode")

    monkeypatch.setattr(zenodo_aliases, "resolve_zenodo", boom)
    report = daily.run_daily(make_ctx(stub_fetchers(**_by_source(sample_records)), offline=True),
                             log=lambda *_: None)
    assert report.ok
