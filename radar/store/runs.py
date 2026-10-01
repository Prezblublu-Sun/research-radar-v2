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
