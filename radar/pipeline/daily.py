"""The daily run: compose the stages, decide the verdict, write the files.

Replaces v1's 436-line ``run()``. Each stage is a function with explicit
inputs and outputs (see the sibling modules); this module only wires them
and decides ``run_status`` / ``quality_flags``.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass

from radar.core.records import utc_now_iso
from radar.pipeline import dedup as _dedup
from radar.pipeline import fetch as _fetch
from radar.pipeline import health as _health
from radar.pipeline import manifest as _manifest
from radar.pipeline import persist as _persist
from radar.pipeline import random_reading as _random
from radar.pipeline import router as _router
from radar.pipeline.context import RunContext
from radar.sources import zenodo_aliases
from radar.store.seen import SeenKeys


class _Offline(Exception):
    pass


@dataclass
class RunReport:
    run_path: pathlib.Path | None
    random_path: pathlib.Path | None
    header: dict
    papers: list[dict]
    random_papers: list[dict]
    blocking: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.blocking


def priority_counts(papers: list[dict]) -> tuple[dict[str, int], int]:
    counts = {"High": 0, "Medium": 0, "Low": 0, "Exclude": 0}
    failed = 0
    for paper in papers:
        llm = paper.get("llm") or {}
        if llm.get("scorer_failed") is True:
            failed += 1
            continue
        priority = llm.get("priority")
        if priority in counts:
            counts[priority] += 1
        elif priority is None and llm.get("dry_run"):
            continue
        else:
            counts["Low"] += 1  # legacy null -> Low contract for odd records
    return counts, failed


def _resolve_aliases(ctx: RunContext, fetched, log) -> dict:
    """Zenodo concept-DOI aliases; a lookup failure must not stall the run."""
    alias_cache = ctx.data_root.aliases / "zenodo.json"
    if ctx.offline:
        return zenodo_aliases.flat(zenodo_aliases.load(alias_cache))
    try:
        aliases, report = zenodo_aliases.resolve_zenodo(
            (p.get("doi") for lst in fetched.lists for p in lst), alias_cache)
        if report["looked_up"]:
            log(f"  Zenodo concept-DOI lookups: {report}")
        return aliases
    except Exception as error:  # noqa: BLE001
        log(f"  ! DOI alias resolution skipped: {error}")
        return {}


def _random_stage(ctx: RunContext, scored, seen: SeenKeys, new_keys: set[str],
                  fetched_total: int, raws: list[dict], log) -> tuple[list[dict], dict | None, dict]:
    """Returns ``(picks, report_or_None, summary)``; never raises."""
    if not ctx.random_reading:
        return [], None, {"status": "disabled"}
    if ctx.offline:
        return [], None, {"status": "skipped_offline"}
    if fetched_total == 0:
        return [], None, {"status": "skipped_empty_run"}
    if ctx.scorer.budget_exhausted:
        return [], None, {"status": "skipped_budget_exhausted"}
    try:
        known = set(seen.keys) | set(new_keys) | _random.drawn_keys(ctx.data_root)
        picks, report = _random.collect(scored, known, ctx.today_iso, ctx.config,
                                        fetcher=ctx.fetchers.openalex)
        log(f"Random reading: {len(report['journals'])} journal(s), {len(picks)} paper(s), "
            f"{report['errors']} error(s)")
        if picks:
            picks, rr_raws = ctx.scorer.score_batch(picks, ctx.config)
            raws.extend(rr_raws)
        counts, failed = priority_counts(picks)
        return picks, report, {"status": "ok", "journals": len(report["journals"]),
                               "papers": len(picks), "errors": report["errors"],
                               "priority_counts": counts, "scorer_failed": failed}
    except Exception as error:  # noqa: BLE001 — serendipity must never fail the run
        log(f"  ! random reading failed: {type(error).__name__}: {error}")
        return [], None, {"status": "failed", "error": f"{type(error).__name__}: {error}"[:200]}


def run_daily(ctx: RunContext, *, seen: SeenKeys | None = None, log=print) -> RunReport:
    started = utc_now_iso()
    log(f"[{ctx.run_id}] {ctx.run_type} run, today={ctx.today_iso}, days_back={ctx.days_back}"
        + (" (DRY RUN)" if ctx.dry_run else ""))

    fetched = _fetch.fetch_stage(ctx, log=log)
    quality_flags: list[str] = []
    for name, count in fetched.source_counts.items():
        if count == 0:
            quality_flags.append(f"{name}_returned_zero")
    for name in fetched.errors:
        quality_flags.append(f"{name}_failed")
    if fetched.openalex_stats.get("truncated"):
        quality_flags.append("openalex_truncated")

    aliases = _resolve_aliases(ctx, fetched, log)

    seen = seen or SeenKeys.load(ctx.data_root)
    log(f"Dedup against {len(seen)} known works" + (" (rebuilt)" if seen.rebuilt else ""))
    deduped = _dedup.dedup_stage(fetched.lists, seen.keys, aliases, force=ctx.force)
    log(f"  -> {len(deduped.papers)} new, {deduped.already_seen} already known, "
        f"{deduped.merged} merged across sources")

    for paper in deduped.papers:
        _router.route(paper, ctx.config.directions, ctx.config.exclusions)
    routed = _router.filter_routed(deduped.papers)
    by_direction = {key: 0 for key in ctx.config.keys}
    for paper in routed:
        by_direction[paper["direction"]] += 1
    log(f"Routed {len(routed)}/{len(deduped.papers)}: "
        + ", ".join(f"{k}={v}" for k, v in by_direction.items()))

    log(f"Scoring {len(routed)} papers with {ctx.scorer.version}")
    scored, raws = ctx.scorer.score_batch(routed, ctx.config)
    boosted = _router.apply_crossover_boost(scored, ctx.config.crossover_pairs)
    counts_by_priority, scorer_failed = priority_counts(scored)
    log(f"  -> {counts_by_priority}, failed {scorer_failed}, boosted {boosted}")

    random_picks, random_report, random_summary = _random_stage(
        ctx, scored, seen, deduped.new_keys, fetched.fetched_total, raws, log)

    usage = _manifest.summarize_llm_usage(raws)
    if usage["usage"]["calls"]:
        log(f"  -> LLM: {usage['usage']['calls']} calls, ~${usage['estimated_usd']} "
            f"({usage['price_window']})")

    if ctx.scorer.budget_exhausted:
        quality_flags.append("scorer_budget_exhausted")
        log(f"::warning::DeepSeek balance exhausted: {ctx.scorer.budget_exhausted}")
    if scorer_failed:
        quality_flags.append("scorer_failed")
    if fetched.fetched_total and fetched.fetched_total < 50:
        quality_flags.append("low_fetch_count")
    if fetched.fetched_total and not routed:
        quality_flags.append("zero_routed")
    if routed and counts_by_priority["High"] + counts_by_priority["Medium"] == 0 and not ctx.dry_run:
        quality_flags.append("zero_high_medium")

    run_status = "partial_success" if fetched.errors else "success"
    if fetched.fetched_total == 0:
        run_status = "failed"
        quality_flags.append("fetched_zero")

    counts = {
        "fetched": fetched.fetched_total,
        "by_source": dict(fetched.source_counts),
        "after_dedup": len(deduped.papers),
        "already_seen": deduped.already_seen,
        "after_routing": len(routed),
        "by_direction": by_direction,
        "priority_counts": counts_by_priority,
        "scorer_failed": scorer_failed,
        "boosted": boosted,
        "records_written": len(scored) if fetched.fetched_total else 0,
    }
    model_snapshot = next((r.get("_raw_model") for r in raws if r.get("_raw_model")), "")
    header = _manifest.build_header(
        ctx, started_at=started, run_status=run_status, quality_flags=quality_flags,
        window=fetched.window, sources_used=fetched.sources_used,
        source_status=fetched.source_status, counts=counts, raws=raws,
        random_reading=random_summary, model_snapshot=model_snapshot,
    )

    run_path = None
    random_path = None
    if fetched.fetched_total == 0:
        log("ABORT: every source returned nothing; no run file written (empty-run guard)")
    else:
        run_path = _persist.persist_stage(ctx, header, scored, aliases=aliases)
        log(f"Wrote {len(scored)} records -> {run_path}")
        if random_report and random_report["journals"]:
            random_header = _random.build_header(ctx, random_picks, random_report["journals"])
            random_path = _random.persist_random(ctx, random_header, random_picks, aliases=aliases)
            log(f"Wrote {len(random_picks)} random-reading records -> {random_path}")
        if not ctx.dry_run:
            seen.keys |= deduped.new_keys
            seen.through, seen.n_files = run_path.name, seen.n_files + 1
            seen.save_cache(ctx.data_root)

    verdict = _health.evaluate(header)
    for line in verdict.warnings:
        log(f"::warning::{line}")
    for line in verdict.blocking:
        log(f"::error::{line}")
    return RunReport(run_path=run_path, random_path=random_path, header=header,
                     papers=scored, random_papers=random_picks,
                     blocking=verdict.blocking, warnings=verdict.warnings)
