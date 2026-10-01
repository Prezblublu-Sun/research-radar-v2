"""Stage: write the run log. The only stage that must raise.

One JSON Lines file per run, header first, never rewritten. ``ck`` is the
first key of every paper line on purpose: ``radar.store.runs.scan_keys``
reads it off the line prefix with a regex and never has to parse JSON.
"""
from __future__ import annotations

import pathlib

from radar.core import identity
from radar.core.atomic import write_jsonl_new
from radar.core.records import (ABSTRACT_MAX_CHARS, RECORD_DROPPED_KEYS,
                                SCHEMA_VERSION, infer_date_precision, utc_now_iso)
from radar.pipeline.router import crossovers


def build_paper_record(paper: dict, ctx, *, aliases: dict | None = None,
                       scored_at: str | None = None,
                       provenance: dict | None = None) -> dict:
    """The line written for one paper. ``ck`` first, v1 keys flat, v2 keys added."""
    ck = identity.canonical_key(paper, aliases)
    body = {k: v for k, v in paper.items() if k not in RECORD_DROPPED_KEYS}
    body["abstract"] = (body.get("abstract") or "")[:ABSTRACT_MAX_CHARS]
    body.setdefault("date_precision", infer_date_precision(body.get("date") or ""))
    record = {
        "ck": ck,
        "kind": "paper",
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "run_type": ctx.run_type,
        "identity_key": identity.identity_key(paper),
        "first_seen_at": paper.get("first_seen_at") or scored_at or utc_now_iso(),
        "scored_at": scored_at or utc_now_iso(),
        "scorer_version": ctx.scorer.version,
        "prompt_sha": ctx.prompt.sha,
    }
    record.update(body)
    record["crossover"] = crossovers(paper, ctx.config.crossover_pairs)
    record["provenance"] = provenance or {"origin": ctx.run_type}
    return record


def persist_stage(ctx, header: dict, papers: list[dict], *,
                  aliases: dict | None = None, scored_at: str | None = None,
                  path: pathlib.Path | None = None) -> pathlib.Path:
    target = path or ctx.data_root.run_file(ctx.run_id, ctx.run_type)
    stamp = scored_at or utc_now_iso()
    lines = [header] + [build_paper_record(p, ctx, aliases=aliases, scored_at=stamp)
                        for p in papers]
    write_jsonl_new(target, lines)
    return target
