"""Import the v1 radar's High/Medium papers, the reader's picks and the
visual registry — once.

Source: a checkout of ``Prezblublu-Sun/research-radar`` (sparse is enough:
``data/daily``, ``data/visuals``, ``data/doi_aliases.json``). v1 stored
papers in per-publication-date buckets, sometimes more than once; this
import keeps one record per canonical key (earliest observation wins, as
v1's own corpus view did), selects every High or Medium paper plus every
paper on the reader's 待读 list whatever its score, re-routes it under the
six v2 directions, and writes month shards to ``data/runs/import-v1/``.

The v1 verdict is carried literally: ``llm`` untouched, no v2 crossover
bump, ``scorer_version`` and ``prompt_sha`` naming the frozen
``scorer_v3.txt``. v1 routing moves into ``provenance``. A High/Medium paper
the v2 router does not route is written to ``data/eval/import-v1-unrouted.jsonl``
instead — read that file before trusting the new keywords; it is a recall
signal, not a discard.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
from dataclasses import dataclass, field

from radar.core import identity
from radar.core.atomic import write_jsonl_new
from radar.core.records import ABSTRACT_MAX_CHARS, SCHEMA_VERSION, infer_date_precision, utc_now_iso
from radar.paths import DataRoot
from radar.pipeline import router
from radar.sources import zenodo_aliases
from radar.store import visuals as _vstore

_BUCKET = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
V1_PROMPT_FILE = "scorer_v3.txt"
V1_ROUTING_KEYS = ("direction", "direction_name", "directions", "routing_matches", "routing_reason",
                   "priority_pre_boost")
V1_DROPPED_KEYS = ("concepts", "schema_version", "scorer_version")


def load_v1_buckets(daily_dir: pathlib.Path):
    """Yield ``(bucket_date, position, paper)`` from every bucket; skips the
    ``*.SKIPPED.json`` sentinels and tolerates the pre-migration list shape."""
    for path in sorted(pathlib.Path(daily_dir).glob("*.json")):
        if not _BUCKET.fullmatch(path.name):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        papers = data if isinstance(data, list) else (data.get("papers") if isinstance(data, dict) else None)
        for position, paper in enumerate(papers or []):
            if isinstance(paper, dict):
                yield path.stem, position, paper


def _rank(bucket_date: str, paper: dict, position: int) -> tuple:
    first_seen = str(paper.get("first_seen_at") or "").strip()
    return (0 if not first_seen else 1, first_seen, bucket_date, str(paper.get("source") or ""), position)


def canonicalize(observations, aliases: dict | None = None) -> tuple[dict[str, dict], int]:
    """``ck -> (bucket_date, paper)`` keeping the earliest observation; the
    count of observations dropped as duplicates is returned too."""
    winners: dict[str, tuple[tuple, str, dict]] = {}
    raw = 0
    for bucket_date, position, paper in observations:
        raw += 1
        ck = identity.canonical_key(paper, aliases) or identity.identity_key(paper)
        if not ck:
            continue   # identity-less v1 records are not worth importing
        candidate = (_rank(bucket_date, paper, position), bucket_date, paper)
        current = winners.get(ck)
        if current is None or candidate[0] < current[0]:
            winners[ck] = candidate
    return {ck: (b, p) for ck, (_, b, p) in winners.items()}, raw - len(winners)


def load_pick_keys(path: pathlib.Path | None) -> set[str]:
    if not path or not pathlib.Path(path).exists():
        return set()
    keys = set()
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            ck = json.loads(line).get("ck")
            if ck:
                keys.add(ck)
    return keys


@dataclass
class ImportReport:
    raw: int = 0
    unique: int = 0
    selected: int = 0
    by_v1_priority: dict[str, int] = field(default_factory=dict)
    routed: int = 0
    unrouted: int = 0
    picks_kept_unrouted: int = 0
    by_direction: dict[str, int] = field(default_factory=dict)
    unrouted_by_v1_direction: dict[str, int] = field(default_factory=dict)
    shards: list[str] = field(default_factory=list)
    visuals: int = 0
    aliases: int = 0


def to_v2_record(ck: str, bucket_date: str, paper: dict, config, *, prompt_sha: str, imported_at: str,
                 keep_unrouted: bool) -> tuple[dict | None, bool]:
    """``(record, routed)``; record is None when unrouted and not kept."""
    body = {k: v for k, v in paper.items() if k not in V1_ROUTING_KEYS and k not in V1_DROPPED_KEYS}
    body["abstract"] = (body.get("abstract") or "")[:ABSTRACT_MAX_CHARS]
    body.setdefault("date", bucket_date)
    body.setdefault("date_precision", infer_date_precision(body.get("date") or ""))
    probe = {"title": paper.get("title", ""), "abstract": paper.get("abstract", "")}
    router.route(probe, config.directions, config.exclusions)
    routed = bool(probe.get("direction"))
    if not routed and not keep_unrouted:
        return None, False
    body["directions"] = probe.get("directions") or []
    body["direction"] = probe.get("direction")
    body["direction_name"] = probe.get("direction_name")
    body["routing_matches"] = probe.get("routing_matches") or {}
    if not routed:
        body["routing_reason"] = "eval pick"
    first_seen = paper.get("first_seen_at") or f"{bucket_date}T00:00:00Z"
    record = {
        "ck": ck,
        "kind": "paper",
        "schema_version": SCHEMA_VERSION,
        "run_id": f"import-v1-{bucket_date[:7]}",
        "run_type": "import_v1",
        "identity_key": identity.identity_key(paper),
        "first_seen_at": first_seen,
        "scored_at": first_seen,
        "scorer_version": str(paper.get("scorer_version") or "v3"),
        "prompt_sha": prompt_sha,
    }
    record.update(body)
    record["crossover"] = router.crossovers(body, config.crossover_pairs)
    record["provenance"] = {
        "origin": "import_v1", "imported_at": imported_at,
        "v1_direction": paper.get("direction"), "v1_direction_name": paper.get("direction_name"),
        "v1_directions": list(paper.get("directions") or []),
        "v1_bucket_date": bucket_date,
    }
    return record, routed


def import_visuals(old_index: pathlib.Path, root: DataRoot) -> int:
    try:
        payload = json.loads(pathlib.Path(old_index).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict) or not records:
        return 0
    clean = {str(k): v for k, v in records.items() if k and isinstance(v, dict)}
    _vstore.append_visuals_run(root, "import-v1", clean, source="import_v1",
                               path=root.visuals / "import-v1" / "index.jsonl")
    return len(clean)


def import_aliases(old_aliases: pathlib.Path, root: DataRoot) -> int:
    old = zenodo_aliases.load(old_aliases)
    if not old["zenodo"]:
        return 0
    target = root.aliases / "zenodo.json"
    current = zenodo_aliases.load(target)
    merged = {**old["zenodo"], **current["zenodo"]}
    zenodo_aliases.save(target, {"zenodo": merged})
    return len(old["zenodo"])


def run_import(old_repo: pathlib.Path, root: DataRoot, config, *, prompts_dir: pathlib.Path,
               priorities: set[str], picks_path: pathlib.Path | None, dry_run: bool = False,
               log=print) -> ImportReport:
    old_repo = pathlib.Path(old_repo)
    report = ImportReport()
    imported_at = utc_now_iso()
    prompt_text = (pathlib.Path(prompts_dir) / V1_PROMPT_FILE).read_bytes()
    prompt_sha = "sha256:" + hashlib.sha256(prompt_text).hexdigest()[:16]

    aliases_file = old_repo / "data" / "doi_aliases.json"
    aliases = zenodo_aliases.flat(zenodo_aliases.load(aliases_file))
    if not dry_run:
        report.aliases = import_aliases(aliases_file, root)

    winners, duplicates = canonicalize(load_v1_buckets(old_repo / "data" / "daily"), aliases)
    report.raw = len(winners) + duplicates
    report.unique = len(winners)
    picks = load_pick_keys(picks_path)
    log(f"v1 corpus: {report.raw} observations, {report.unique} unique works, {len(picks)} picks")

    shards: dict[str, list[dict]] = {}
    unrouted: list[dict] = []
    for ck, (bucket_date, paper) in sorted(winners.items(), key=lambda kv: (kv[1][0], kv[0])):
        priority = str((paper.get("llm") or {}).get("priority") or "")
        is_pick = ck in picks
        if priority not in priorities and not is_pick:
            continue
        report.selected += 1
        report.by_v1_priority[priority or "none"] = report.by_v1_priority.get(priority or "none", 0) + 1
        record, routed = to_v2_record(ck, bucket_date, paper, config, prompt_sha=prompt_sha,
                                      imported_at=imported_at, keep_unrouted=is_pick)
        if routed:
            report.routed += 1
            report.by_direction[record["direction"]] = report.by_direction.get(record["direction"], 0) + 1
        else:
            report.unrouted += 1
            v1dir = str(paper.get("direction") or "none")
            report.unrouted_by_v1_direction[v1dir] = report.unrouted_by_v1_direction.get(v1dir, 0) + 1
            if record is None:
                unrouted.append({"ck": ck, "identity_key": identity.identity_key(paper), "title": paper.get("title", ""),
                                 "v1_direction": paper.get("direction"), "v1_priority": priority,
                                 "date": bucket_date, "venue": paper.get("venue", "")})
                continue
            report.picks_kept_unrouted += 1
        shards.setdefault(bucket_date[:7], []).append(record)

    log(f"selected {report.selected} ({report.by_v1_priority}); routed {report.routed}, "
        f"unrouted {report.unrouted} (picks kept {report.picks_kept_unrouted})")
    log(f"  by v2 direction: {report.by_direction}")
    log(f"  unrouted by v1 direction: {report.unrouted_by_v1_direction}")
    if dry_run:
        return report

    for month, records in sorted(shards.items()):
        header = {
            "kind": "run", "schema_version": SCHEMA_VERSION, "run_id": f"import-v1-{month}",
            "run_type": "import_v1", "started_at": imported_at, "finished_at": imported_at,
            "git_commit": "", "run_status": "success", "quality_flags": [],
            "config": {"directions_yaml": config.sha, "scorer_prompt_file": V1_PROMPT_FILE,
                       "scorer_prompt": prompt_sha, "scorer_version": "v3"},
            "window": {"mode": "import_v1", "month": month, "source": str(old_repo)},
            "sources_used": {}, "source_status": {}, "llm": {},
            "counts": {"records_written": len(records),
                       "priority_counts": {p: sum(1 for r in records if (r.get("llm") or {}).get("priority") == p)
                                           for p in ("High", "Medium", "Low", "Exclude")}},
            "random_reading": {"status": "disabled"},
        }
        path = root.runs / "import-v1" / f"{month}.jsonl"
        write_jsonl_new(path, [header] + records)
        report.shards.append(str(path))
    if unrouted:
        write_jsonl_new(root.eval / "import-v1-unrouted.jsonl", unrouted)
    if picks_path and pathlib.Path(picks_path).exists():
        target = root.eval / pathlib.Path(picks_path).name
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(pathlib.Path(picks_path).read_bytes())
    report.visuals = import_visuals(old_repo / "data" / "visuals" / "index.json", root)
    log(f"wrote {len(report.shards)} month shard(s), {len(unrouted)} unrouted audit rows, {report.visuals} visual records")
    return report
