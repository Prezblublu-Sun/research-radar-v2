"""Read the run logs. Nothing here writes."""
from __future__ import annotations

import json
import pathlib
import re
from typing import Iterator

from radar.paths import DataRoot

_CK_PREFIX = re.compile(rb'^\{"ck":\s*"([^"]*)"')


def list_runs(root: DataRoot) -> list[pathlib.Path]:
    """Every run log, oldest first (filenames sort chronologically)."""
    if not root.runs.is_dir():
        return []
    return sorted(root.runs.rglob("*.jsonl"))


def run_order(header: dict) -> tuple[str, bool, str]:
    """Newest-run ordering key: when the run finished, then its id.

    Filenames are not the order — the v1 import lives under
    ``runs/import-v1/`` and sorts after every dated year directory — and on
    a tie the import, the oldest evidence by construction, loses.
    """
    return (str(header.get("finished_at") or header.get("started_at") or ""),
            header.get("run_type") != "import_v1", str(header.get("run_id") or ""))


def headers_newest_first(root: DataRoot, *, run_types: set[str] | None = None,
                         limit: int | None = None) -> list[tuple[pathlib.Path, dict]]:
    """``[(path, header)]`` for every readable run log, newest finished first."""
    found: list[tuple[pathlib.Path, dict]] = []
    for path in list_runs(root):
        try:
            header = read_header(path)
        except Exception:  # noqa: BLE001 — one bad file must not hide the rest
            continue
        if run_types is not None and header.get("run_type") not in run_types:
            continue
        found.append((path, header))
    found.sort(key=lambda item: run_order(item[1]), reverse=True)
    return found[:limit] if limit else found


def read_header(path: pathlib.Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        first = handle.readline()
    header = json.loads(first) if first.strip() else {}
    if header.get("kind") != "run":
        raise ValueError(f"{path}: first line is not a run header")
    return header


def iter_records(path: pathlib.Path) -> Iterator[dict]:
    """Paper lines only; the header is skipped."""
    with open(path, "r", encoding="utf-8") as handle:
        for n, line in enumerate(handle):
            if n == 0 or not line.strip():
                continue
            record = json.loads(line)
            if record.get("kind") == "paper":
                yield record


def scan_keys(path: pathlib.Path) -> set[str]:
    """Canonical keys of a run file by regex on each line's prefix.

    ~40x faster than parsing JSON and the reason ``ck`` is always the first
    key of a paper line.
    """
    keys: set[str] = set()
    with open(path, "rb") as handle:
        for line in handle:
            match = _CK_PREFIX.match(line)
            if match and match.group(1):
                keys.add(match.group(1).decode("utf-8"))
    return keys
