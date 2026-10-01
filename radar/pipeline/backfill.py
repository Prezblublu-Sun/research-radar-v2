"""Historical backfill: one ``backfill`` run per calendar month.

Each month goes through the ordinary daily pipeline with a historical
window, so a backfilled month is a run like any other: its own append-only
file, dedup against everything the corpus already holds, the same router
and scorer. Resumable by construction — a month whose backfill run already
exists with a usable status is skipped — and it stops, rather than burns
money, when the DeepSeek budget fuse trips or OpenAlex rate-limits.
"""
from __future__ import annotations

import calendar
import dataclasses
import datetime as dt
from dataclasses import dataclass, field

from radar.core.records import new_run_id
from radar.pipeline import daily as _daily
from radar.pipeline.context import RunContext
from radar.store import runs as _runs
from radar.store.seen import SeenKeys

VALID_SOURCES = {"arxiv", "openalex", "pubmed"}


def iterate_months(from_date: dt.date, to_date: dt.date) -> list[tuple[str, str, str]]:
    """``[(month_key, window_from, window_to)]``; first and last may be partial."""
    if from_date > to_date:
        raise ValueError(f"from_date {from_date} > to_date {to_date}")
    out = []
    year, month, start = from_date.year, from_date.month, from_date
    while True:
        end = min(dt.date(year, month, calendar.monthrange(year, month)[1]), to_date)
        out.append((f"{year:04d}-{month:02d}", start.isoformat(), end.isoformat()))
        if end >= to_date:
            return out
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        start = dt.date(year, month, 1)


def completed_windows(root) -> set[tuple[str, str]]:
    """``(from, to)`` of every backfill run that already produced data."""
    done = set()
    for path in _runs.list_runs(root):
        if "-backfill" not in path.name:
            continue
        try:
            header = _runs.read_header(path)
        except Exception:  # noqa: BLE001
            continue
        window = header.get("window") or {}
        if header.get("run_status") in ("success", "partial_success") and window.get("from") and window.get("to"):
            done.add((window["from"], window["to"]))
    return done


@dataclass
class BackfillReport:
    months: list[dict] = field(default_factory=list)
    stopped: str = ""

    @property
    def ok(self) -> bool:
        return not self.stopped and all(m.get("status") != "failed" for m in self.months)


def run_backfill(base: RunContext, from_date: str, to_date: str, *, log=print) -> BackfillReport:
    report = BackfillReport()
    months = iterate_months(dt.date.fromisoformat(from_date), dt.date.fromisoformat(to_date))
    done = completed_windows(base.data_root)
    seen = SeenKeys.load(base.data_root)
    log(f"Backfill {from_date}..{to_date}: {len(months)} month window(s), {len(done)} already complete")
    for month, w_from, w_to in months:
        if (w_from, w_to) in done:
            log(f"Month {month} ({w_from}..{w_to}): already backfilled, skipping")
            report.months.append({"month": month, "status": "skipped"})
            continue
        ctx = dataclasses.replace(base, run_id=new_run_id(), run_type="backfill",
                                  window_from=w_from, window_to=w_to, random_reading=False)
        result = _daily.run_daily(ctx, seen=seen, log=log)
        entry = {"month": month, "run_id": ctx.run_id, "status": result.header["run_status"],
                 "written": result.header["counts"]["records_written"],
                 "flags": result.header["quality_flags"], "path": str(result.run_path) if result.run_path else ""}
        report.months.append(entry)
        if base.scorer.budget_exhausted:
            report.stopped = f"DeepSeek balance exhausted after {month}"
            break
        if any(result.header["source_status"].get(s, {}).get("error_code") == "OpenAlexRateLimitError"
               for s in ("openalex",)):
            report.stopped = f"OpenAlex rate limit reached in {month}"
            break
    if report.stopped:
        log(f"::warning::backfill stopped early: {report.stopped}")
    return report
