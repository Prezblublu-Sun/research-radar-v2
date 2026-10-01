"""Re-score papers whose newest verdict is a scorer failure.

Nothing is rewritten: the candidates are taken from the merged corpus view,
scored again, and written as one new ``rescore`` run. At read time the
newer, successful verdict wins (ADR-0001 §2.5). A paper that fails again
is written again as failed, so the next rescore still finds it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from radar.core.records import utc_now_iso
from radar.pipeline import manifest as _manifest
from radar.pipeline import persist as _persist
from radar.pipeline import router as _router
from radar.pipeline.context import RunContext
from radar.pipeline.daily import priority_counts
from radar.store import corpus as _corpus


def candidates(corpus: _corpus.Corpus) -> list[dict]:
    """Merged records whose newest verdict is ``scorer_failed``, oldest first."""
    found = [p for p in corpus.papers if _corpus.display_priority(p) == "Unscored"]
    found.sort(key=lambda p: (str(p.get("first_seen_at") or ""), str(p.get("title") or "")))
    return found


@dataclass
class RescoreReport:
    candidates: int = 0
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    run_path: str = ""
    header: dict = field(default_factory=dict)


def run_rescore(ctx: RunContext, *, limit: int | None = None, dry_run: bool = False, log=print) -> RescoreReport:
    corpus = _corpus.load_corpus(ctx.data_root)
    todo = candidates(corpus)
    report = RescoreReport(candidates=len(todo))
    if limit is not None:
        todo = todo[:limit]
    log(f"rescore: {report.candidates} candidate(s), attempting {len(todo)}" + (" (DRY RUN)" if dry_run else ""))
    if dry_run or not todo:
        return report
    started = utc_now_iso()
    papers = [_persist.strip_record_meta(p) for p in todo]
    scored, raws = ctx.scorer.score_batch(papers, ctx.config)
    boosted = _router.apply_crossover_boost(scored, ctx.config.crossover_pairs)
    counts, failed = priority_counts(scored)
    report.attempted, report.failed, report.succeeded = len(scored), failed, len(scored) - failed
    flags = []
    if failed:
        flags.append("scorer_failed")
    if ctx.scorer.budget_exhausted:
        flags.append("scorer_budget_exhausted")
    header = _manifest.build_header(
        ctx, started_at=started, run_status="success", quality_flags=flags,
        window={"mode": "rescore", "candidates": report.candidates, "limit": limit},
        sources_used={}, source_status={},
        counts={"fetched": 0, "by_source": {}, "after_dedup": 0, "already_seen": 0, "after_routing": len(scored),
                "by_direction": {}, "priority_counts": counts, "scorer_failed": failed, "boosted": boosted,
                "records_written": len(scored)},
        raws=raws, random_reading={"status": "disabled"},
        model_snapshot=next((r.get("_raw_model") for r in raws if r.get("_raw_model")), ""))
    path = _persist.persist_stage(ctx, header, scored)
    report.run_path, report.header = str(path), header
    log(f"rescore: {report.succeeded} succeeded, {report.failed} still failed -> {path}")
    return report
