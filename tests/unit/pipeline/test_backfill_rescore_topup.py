"""Backfill months, rescore runs and random-reading top-ups are all new,
append-only runs built on the same stages."""
from __future__ import annotations

import datetime as dt
import json

from radar.core.records import new_run_id
from radar.pipeline import backfill, random_topup, rescore
from radar.pipeline import daily
from radar.pipeline.scorer import ReplayScorer
from radar.store import corpus as _corpus
from radar.store import runs as _runs


def _by_source(records):
    out = {"arxiv": [], "openalex": [], "pubmed": []}
    for r in records:
        out[r["source"]].append(r)
    return out


# --- months ---------------------------------------------------------------------

def test_iterate_months_splits_a_range_into_calendar_windows():
    months = backfill.iterate_months(dt.date(2026, 1, 15), dt.date(2026, 3, 10))
    assert months == [("2026-01", "2026-01-15", "2026-01-31"), ("2026-02", "2026-02-01", "2026-02-28"),
                      ("2026-03", "2026-03-01", "2026-03-10")]
    assert backfill.iterate_months(dt.date(2026, 2, 3), dt.date(2026, 2, 3)) == [("2026-02", "2026-02-03", "2026-02-03")]


def test_run_ids_never_collide_within_a_process():
    ids = {new_run_id() for _ in range(5)}
    assert len(ids) == 5


def test_backfill_writes_one_run_per_month_in_window_mode_and_resumes(make_ctx, stub_fetchers, sample_records, data_root):
    fetchers = stub_fetchers(**_by_source(sample_records))
    base = make_ctx(fetchers, scorer=ReplayScorer({}, default={"priority": "Medium"}))
    report = backfill.run_backfill(base, "2026-08-20", "2026-09-05", log=lambda *_: None)
    assert report.ok and [m["month"] for m in report.months] == ["2026-08", "2026-09"]
    assert fetchers.calls["arxiv"] == {"from_date": "2026-09-01", "to_date": "2026-09-05"}   # last call = second month
    assert "days_back" not in fetchers.calls["openalex"]
    files = _runs.list_runs(data_root)
    assert len(files) == 2 and all(p.name.endswith("-backfill.jsonl") for p in files)
    first, second = (_runs.read_header(p) for p in files)
    assert first["window"] == {"mode": "historical", "from": "2026-08-20", "to": "2026-08-31"}
    assert first["random_reading"] == {"status": "disabled"}
    assert first["counts"]["records_written"] > 0 and second["counts"]["already_seen"] == first["counts"]["after_dedup"]

    again = backfill.run_backfill(base, "2026-08-20", "2026-09-05", log=lambda *_: None)
    assert [m["status"] for m in again.months] == ["skipped", "skipped"]
    assert len(_runs.list_runs(data_root)) == 2


def test_backfill_stops_when_the_budget_fuse_trips(make_ctx, stub_fetchers, sample_records, data_root):
    class Tripping(ReplayScorer):
        def score_batch(self, papers, config):
            out = super().score_batch(papers, config)
            self.budget_exhausted = "Insufficient Balance"
            return out
    base = make_ctx(stub_fetchers(**_by_source(sample_records)), scorer=Tripping({}, default={"priority": "Low"}))
    report = backfill.run_backfill(base, "2026-07-01", "2026-09-30", log=lambda *_: None)
    assert report.stopped.startswith("DeepSeek") and len(report.months) == 1 and not report.ok


# --- rescore ---------------------------------------------------------------------

def test_rescore_writes_a_new_run_that_supersedes_failures(make_ctx, stub_fetchers, sample_records, data_root):
    fetchers = stub_fetchers(**_by_source(sample_records))
    failing = ReplayScorer({}, default={"priority": None, "scorer_failed": True, "scorer_failed_reason": "x",
                                        "scorer_failed_attempts": 3})
    first = daily.run_daily(make_ctx(fetchers, scorer=failing), log=lambda *_: None)
    assert first.header["counts"]["scorer_failed"] == len(first.papers) > 0

    ctx = make_ctx(fetchers, run_id="2026-10-02T120000Z", run_type="rescore",
                   scorer=ReplayScorer({}, default={"priority": "High", "tags": ["fixed"]}))
    dry = rescore.run_rescore(ctx, dry_run=True, log=lambda *_: None)
    assert dry.candidates == len(first.papers) and dry.attempted == 0 and len(_runs.list_runs(data_root)) == 1

    report = rescore.run_rescore(ctx, limit=5, log=lambda *_: None)
    assert report.attempted == 5 and report.succeeded == 5 and report.failed == 0
    header = _runs.read_header(data_root.run_file(ctx.run_id, "rescore"))
    assert header["run_type"] == "rescore" and header["counts"]["records_written"] == 5
    for record in _runs.iter_records(data_root.run_file(ctx.run_id, "rescore")):
        assert record["provenance"] == {"origin": "rescore"} and record["llm"]["priority"] == "High"
        assert "runs" not in record and "first_run_id" not in record
    c = _corpus.load_corpus(data_root)
    assert c.stats.unique_total == len(first.papers)
    assert c.stats.priority_counts["High"] == 5 and c.stats.priority_counts["Unscored"] == len(first.papers) - 5
    assert rescore.candidates(c) and len(rescore.candidates(c)) == len(first.papers) - 5


# --- random top-up -----------------------------------------------------------------

class _JournalStub:
    PAGE_LIMIT = 10_000

    def __init__(self, inner):
        self.inner = inner

    def fetch(self, *a, **k):
        return self.inner.fetch(*a, **k)

    def journal_month_count(self, sid, f, t):
        return 40

    def journal_work_at(self, sid, f, t, pos):
        return {"source": "openalex", "id": f"https://openalex.org/WR{pos}", "doi": f"10.9/r-{sid}-{pos}",
                "title": f"Random {pos}", "abstract": "", "authors": [], "venue": "J", "venue_id": sid,
                "venue_type": "journal", "date": f, "year": 2026}


def test_random_topup_fills_a_gap_then_tops_up_without_redrawing(make_ctx, stub_fetchers, sample_records, data_root, monkeypatch):
    from radar.pipeline.context import Fetchers
    inner = stub_fetchers(**_by_source(sample_records))
    fetchers = Fetchers(arxiv=inner.arxiv, openalex=_JournalStub(inner.openalex), pubmed=inner.pubmed)
    high = ReplayScorer({}, default={"priority": "High"})
    # a daily run on 2026-10-01 with random reading disabled: the gap to fill
    run = daily.run_daily(make_ctx(fetchers, scorer=high, random_reading=False), log=lambda *_: None)
    journals = {p["venue_id"] for p in run.papers if p.get("venue_type") == "journal" and p.get("venue_id")}
    assert journals

    base = make_ctx(fetchers, scorer=high, random_reading=True)
    dry = random_topup.run_random_topup(base, ["2026-10-01", "2026-10-02"], dry_run=True, log=lambda *_: None)
    assert dry.days[0]["status"] == "dry_run" and dry.days[1]["status"] == "no_run" and not data_root.random_reading.exists()

    monkeypatch.setattr(random_topup._random, "SPECIALIST_PICKS", 2)
    filled = random_topup.run_random_topup(base, ["2026-10-01"], log=lambda *_: None)
    assert filled.days[0]["status"] == "written" and filled.papers == 2 * min(len(journals), 6)
    skipped = random_topup.run_random_topup(base, ["2026-10-01"], log=lambda *_: None)
    assert skipped.days[0]["status"] == "skipped"

    monkeypatch.setattr(random_topup._random, "SPECIALIST_PICKS", 5)
    topped = random_topup.run_random_topup(base, ["2026-10-01"], top_up=True, log=lambda *_: None)
    assert topped.days[0]["status"] == "written" and topped.papers == 3 * min(len(journals), 6)
    files = sorted(data_root.random_reading.rglob("*.jsonl"))
    assert len(files) == 2
    headers = [json.loads(p.read_text(encoding="utf-8").splitlines()[0]) for p in files]
    assert headers[0]["top_up_of"] == "" and headers[1]["top_up_of"] == headers[0]["run_id"]
    assert headers[1]["date"] == headers[0]["date"] == "2026-10-01"
    # the two draws never overlap: the stream remembers its own picks
    keys = [_runs.scan_keys(p) for p in files]
    assert keys[0].isdisjoint(keys[1])
