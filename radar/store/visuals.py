"""The visuals registry as an append-only stream.

v1 kept one 3.3 MB ``index.json`` rewritten on every enrichment commit. Here
every enrichment run appends one file, ``data/visuals/YYYY/<run_id>.jsonl``
(header line, then one record per identity with ``identity_key`` first),
and the registry is derived: for each identity the record with the newest
``checked_at`` wins, later file breaking ties. The v1 import lands once at
``data/visuals/import-v1/index.jsonl`` and is simply the oldest file.
"""
from __future__ import annotations

import json
import pathlib

from radar.core.atomic import write_jsonl_new
from radar.core.records import utc_now_iso
from radar.paths import DataRoot

SCHEMA_VERSION = 1


def visuals_file(root: DataRoot, run_id: str) -> pathlib.Path:
    return root.visuals / run_id[:4] / f"{run_id}.jsonl"


def list_files(root: DataRoot) -> list[pathlib.Path]:
    return sorted(root.visuals.rglob("*.jsonl")) if root.visuals.is_dir() else []


def append_visuals_run(root: DataRoot, run_id: str, records: dict[str, dict], *,
                       source: str = "enrich", path: pathlib.Path | None = None) -> pathlib.Path:
    """Write one new file with these ``identity_key -> visual`` records."""
    target = path or visuals_file(root, run_id)
    header = {"kind": "visuals", "schema_version": SCHEMA_VERSION, "run_id": run_id,
              "generated_at": utc_now_iso(), "source": source, "counts": {"records": len(records)}}
    lines = [header] + [{"identity_key": key, **{k: v for k, v in record.items() if k != "identity_key"}}
                        for key, record in sorted(records.items())]
    write_jsonl_new(target, lines)
    return target


def latest_visuals(root: DataRoot) -> dict[str, dict]:
    """``identity_key -> newest record`` across the whole stream."""
    latest: dict[str, dict] = {}
    for path in list_files(root):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for n, line in enumerate(handle):
                    if n == 0 or not line.strip():
                        continue
                    record = json.loads(line)
                    key = record.get("identity_key")
                    if not key:
                        continue
                    current = latest.get(key)
                    if current is None or str(record.get("checked_at") or "") >= str(current.get("checked_at") or ""):
                        latest[key] = {k: v for k, v in record.items() if k != "identity_key"}
        except (OSError, ValueError):
            continue   # one damaged file must not empty every card's figure
    return latest
