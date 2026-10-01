"""Serendipity pass: read the journals that produced today's High papers.

Ported from v1 (ADR-0035). The radar finds papers by keyword and concept,
so it only ever shows work that already looks like the reader's own. A
journal that just produced a High paper is, by that day's evidence,
publishing in the right neighbourhood — and most of what it publishes that
month never matches a keyword. So: take the journals of today's High
papers, draw a few of each journal's papers from the current month at
random, score them with the ordinary prompt, and keep them in a stream of
their own. They are graded honestly (most come back Low, and that is the
point of reading outside the keywords) and they are **not** part of the
corpus: never in the seen set, never in the queue, never in the counts.

The draw scales *inversely* with the journal's monthly output: a specialist
journal gives five, a megajournal above 200 papers/month gives two, because
over a week every megajournal draw scored Exclude. The draw is seeded by
date and journal so a re-run repeats it instead of quietly buying a
different sample.

Storage: ``data/random_reading/YYYY/<run_id>.jsonl`` — header line, then
paper lines in the usual record shape with ``provenance.origin = "random"``
and a ``random_reading`` block naming the journal and the seed paper. A
top-up writes a new file whose header carries ``top_up_of``.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import pathlib
import random

from radar.core import identity
from radar.core.atomic import write_jsonl_new
from radar.core.records import SCHEMA_VERSION, utc_now_iso
from radar.paths import DataRoot
from radar.pipeline import persist as _persist
from radar.pipeline import router
from radar.store.runs import scan_keys

MAX_JOURNALS = 6
MEGAJOURNAL_WORKS = 200
SPECIALIST_PICKS = 5
MEGAJOURNAL_PICKS = 2
# Extra positions to look at beyond the target so papers the radar already
# holds can be skipped without another round trip. A well-covered 15-paper
# journal is effectively scanned; a megajournal still costs a dozen calls.
FETCH_HEADROOM = 10
JOURNAL_SOURCE_TYPES = {"journal"}   # preprint servers have no monthly issue
DEFAULT_PAGE_LIMIT = 10_000


def picks_for_volume(month_works: int) -> int:
    if month_works <= 0:
        return 0
    return MEGAJOURNAL_PICKS if month_works > MEGAJOURNAL_WORKS else SPECIALIST_PICKS


def month_window(today: str) -> tuple[str, str]:
    """First of the month through the month end: publishers deposit ahead."""
    day = dt.date.fromisoformat(today)
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1).isoformat(), day.replace(day=last).isoformat()


def journals_of_high_papers(scored: list[dict]) -> list[dict]:
    """Distinct journals behind today's High papers, with the seed paper."""
    journals: dict[str, dict] = {}
    for paper in scored:
        if ((paper.get("llm") or {}).get("priority")) != "High":
            continue
        source_id = (paper.get("venue_id") or "").strip()
        if not source_id or (paper.get("venue_type") or "") not in JOURNAL_SOURCE_TYPES:
            continue
        if source_id in journals:
            journals[source_id]["seed_count"] += 1
            continue
        journals[source_id] = {
            "venue_id": source_id,
            "venue": paper.get("venue") or source_id,
            "issn_l": paper.get("venue_issn_l") or "",
            "direction": paper.get("direction"),
            "seed_title": paper.get("title") or "",
            "seed_identity_key": identity.canonical_key(paper) or "",
            "seed_count": 1,
        }
    return list(journals.values())[:MAX_JOURNALS]


def random_file(root: DataRoot, run_id: str) -> pathlib.Path:
    return root.random_reading / run_id[:4] / f"{run_id}.jsonl"


def drawn_keys(root: DataRoot) -> set[str]:
    """Every paper this stream has ever drawn (it is kept out of the corpus
    seen set, so the stream has to remember its own history)."""
    keys: set[str] = set()
    if root.random_reading.is_dir():
        for path in sorted(root.random_reading.rglob("*.jsonl")):
            keys |= scan_keys(path)
    return keys


def _rng(today: str, source_id: str) -> random.Random:
    digest = hashlib.sha256(f"{today}:{source_id}".encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def sample_journal(journal: dict, today: str, known_keys: set[str],
                   fetcher, have: int = 0) -> tuple[list[dict], dict]:
    """Draw unseen papers from this journal's month, scaled to its volume.

    ``have`` is how many papers the journal already contributed today, so a
    top-up continues the same seeded ordering instead of redrawing. Any
    OpenAlex failure is reported, never raised.
    """
    from_date, to_date = month_window(today)
    report = {
        "venue_id": journal["venue_id"], "venue": journal["venue"],
        "issn_l": journal.get("issn_l", ""),
        "seed_title": journal.get("seed_title", ""),
        "seed_identity_key": journal.get("seed_identity_key", ""),
        "seed_count": journal.get("seed_count", 1),
        "direction": journal.get("direction"),
        "month": today[:7], "month_works": 0, "target_picks": 0,
        "positions": [], "skipped_known": 0, "truncated_pool": False, "error": "",
    }
    try:
        total = int(fetcher.journal_month_count(journal["venue_id"], from_date, to_date))
    except Exception as error:  # noqa: BLE001 — serendipity must not fail the run
        report["error"] = f"count failed: {type(error).__name__}: {error}"[:200]
        return [], report
    report["month_works"] = total
    if total <= 0:
        return [], report
    page_limit = int(getattr(fetcher, "PAGE_LIMIT", DEFAULT_PAGE_LIMIT))
    reachable = min(total, page_limit)
    report["truncated_pool"] = total > reachable

    target = min(picks_for_volume(total), reachable)
    report["target_picks"] = target
    wanted = max(0, target - max(0, have))
    if not wanted:
        return [], report
    # random.sample builds a partial shuffle, so the same seed yields the
    # same prefix whatever k is: a top-up walks further down the same list.
    order = _rng(today, journal["venue_id"]).sample(
        range(1, reachable + 1), k=min(reachable, target + FETCH_HEADROOM))

    picks: list[dict] = []
    for position in order:
        if len(picks) >= wanted:
            break
        try:
            work = fetcher.journal_work_at(journal["venue_id"], from_date, to_date, position)
        except Exception as error:  # noqa: BLE001
            report["error"] = f"draw failed: {type(error).__name__}: {error}"[:200]
            break
        if not work:
            continue
        key = identity.canonical_key(work)
        if key and key in known_keys:
            report["skipped_known"] += 1
            continue
        if key:
            known_keys.add(key)
        work["random_reading"] = {
            "venue": journal["venue"], "venue_id": journal["venue_id"],
            "issn_l": journal.get("issn_l", ""), "month": today[:7],
            "month_works": total, "position": position,
            "seed_title": journal.get("seed_title", ""),
            "seed_identity_key": journal.get("seed_identity_key", ""),
        }
        picks.append(work)
        report["positions"].append(position)
    return picks, report


def collect(scored: list[dict], known_keys: set[str], today: str, config,
            fetcher, have: dict[str, int] | None = None) -> tuple[list[dict], dict]:
    """This run's random reading: ``(papers, report)``. ``known_keys`` is
    mutated so one journal cannot hand back another's paper."""
    report = {"journals": [], "papers": 0, "errors": 0}
    picks: list[dict] = []
    for journal in journals_of_high_papers(scored):
        drawn, journal_report = sample_journal(
            journal, today, known_keys, fetcher, have=(have or {}).get(journal["venue_id"], 0))
        report["journals"].append(journal_report)
        if journal_report["error"]:
            report["errors"] += 1
        for paper in drawn:
            router.route(paper, config.directions, config.exclusions)
            if not paper.get("direction"):
                # Never send a paper to the scorer with no focus at all.
                paper["direction"] = journal["direction"]
                paper["direction_name"] = config.display_name(journal["direction"]) if journal["direction"] else None
                paper["routing_reason"] = "inherited from the seed High paper"
            picks.append(paper)
    report["papers"] = len(picks)
    return picks, report


def build_header(ctx, picks: list[dict], journals: list[dict], *,
                 top_up_of: str = "") -> dict:
    return {
        "kind": "random_reading",
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "run_type": ctx.run_type,
        "date": ctx.today_iso,
        "month": ctx.today_iso[:7],
        "generated_at": utc_now_iso(),
        "scorer_version": ctx.scorer.version,
        "prompt_sha": ctx.prompt.sha,
        "top_up_of": top_up_of,
        "journals": journals,
        "counts": {"journals": len(journals), "papers": len(picks),
                   "errors": sum(1 for j in journals if j.get("error"))},
    }


def persist_random(ctx, header: dict, picks: list[dict], *, aliases: dict | None = None,
                   path: pathlib.Path | None = None) -> pathlib.Path:
    target = path or random_file(ctx.data_root, ctx.run_id)
    stamp = utc_now_iso()
    lines = [header]
    for paper in picks:
        block = paper.get("random_reading") or {}
        lines.append(_persist.build_paper_record(
            paper, ctx, aliases=aliases, scored_at=stamp,
            provenance={"origin": "random", "venue_id": block.get("venue_id", ""),
                        "seed_identity_key": block.get("seed_identity_key", "")}))
    write_jsonl_new(target, lines)
    return target
