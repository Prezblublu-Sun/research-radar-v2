"""Random reading for past run-days: fill gaps, or top a day up after a rule
change. Each day that gets anything new becomes a NEW file in the random
stream whose header says which run it tops up; nothing is rewritten.

A day is the calendar date of its daily run(s). Its High papers are the
ones those runs introduced, read from the merged corpus so a later rescore
counts. A day that already has random reading is skipped unless
``top_up`` is set, in which case only the shortfall against the current
allocation rule is drawn, continuing the same seeded ordering.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field

from radar.core.records import new_run_id
from radar.pipeline import random_reading as _random
from radar.pipeline.context import RunContext
from radar.store import corpus as _corpus
from radar.store.seen import SeenKeys


def run_days(from_date: str, to_date: str) -> list[str]:
    start, end = dt.date.fromisoformat(from_date), dt.date.fromisoformat(to_date)
    return [(start + dt.timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]


def existing_for_day(root, day: str) -> tuple[str, dict[str, int]]:
    """``(run_id of the original random file, picks held per journal)`` for a day."""
    original, held = "", {}
    for header, records in _corpus.load_random_runs(root, limit=10_000):
        if header.get("date") != day:
            continue
        if not header.get("top_up_of") and not original:
            original = header.get("run_id") or ""
        for record in records:
            venue = (record.get("random_reading") or {}).get("venue_id") or ""
            if venue:
                held[venue] = held.get(venue, 0) + 1
    return original, held


@dataclass
class TopupReport:
    days: list[dict] = field(default_factory=list)
    papers: int = 0
    stopped: str = ""


def run_random_topup(base: RunContext, days: list[str], *, top_up: bool = False, dry_run: bool = False,
                     log=print) -> TopupReport:
    report = TopupReport()
    corpus = _corpus.load_corpus(base.data_root)
    by_run = corpus.by_first_run
    daily_runs = [h for h in corpus.headers if h.get("run_type") == "daily"]
    known = set(SeenKeys.load(base.data_root).keys) | _random.drawn_keys(base.data_root)
    for day in days:
        run_ids = [h["run_id"] for h in daily_runs if str(h.get("run_id", "")).startswith(day)]
        if not run_ids:
            log(f"{day}: no daily run")
            report.days.append({"day": day, "status": "no_run"})
            continue
        original, held = existing_for_day(base.data_root, day)
        if original and not top_up:
            log(f"{day}: already has random reading ({original}), skipping")
            report.days.append({"day": day, "status": "skipped"})
            continue
        high = [p for rid in run_ids for p in by_run.get(rid, []) if (p.get("llm") or {}).get("priority") == "High"]
        if not high:
            log(f"{day}: no High papers")
            report.days.append({"day": day, "status": "no_high"})
            continue
        picks, rr = _random.collect(high, known, day, base.config, fetcher=base.fetchers.openalex, have=held)
        names = ", ".join(f"{j['venue']}({j['month_works']}→{j['target_picks']}"
                          + (f", 已有{held.get(j['venue_id'], 0)}" if held.get(j["venue_id"]) else "") + ")"
                          for j in rr["journals"]) or "(none)"
        log(f"{day}: {len(high)} High, {len(rr['journals'])} journal(s) -> {len(picks)} new paper(s) [{names}]")
        if not picks:
            report.days.append({"day": day, "status": "nothing_to_add", "journals": len(rr["journals"])})
            continue
        if dry_run:
            for paper in picks:
                log(f"    - {str(paper.get('title') or '')[:88]}")
            report.days.append({"day": day, "status": "dry_run", "papers": len(picks)})
            report.papers += len(picks)
            continue
        ctx = dataclasses.replace(base, run_id=new_run_id(), today=dt.date.fromisoformat(day))
        scored, _ = ctx.scorer.score_batch(picks, ctx.config)
        header = _random.build_header(ctx, scored, rr["journals"], top_up_of=original if top_up else "")
        path = _random.persist_random(ctx, header, scored)
        report.papers += len(scored)
        report.days.append({"day": day, "status": "written", "papers": len(scored), "path": str(path)})
        log(f"    -> {path}")
        if ctx.scorer.budget_exhausted:
            report.stopped = f"DeepSeek balance exhausted after {day}"
            log(f"::warning::{report.stopped}")
            break
    return report
