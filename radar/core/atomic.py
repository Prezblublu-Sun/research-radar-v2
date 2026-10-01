"""Writes that cannot leave a half-written file behind.

Every write in v2 goes through one of these: tmp file in the same directory,
flushed and fsynced, then ``os.replace``. v1 did this for most files and not
for its dedup state, whose truncation would have silently reset dedup for
the whole fetch window — the exact class of the 2026-05-12 incident.

``write_jsonl_new`` additionally refuses to overwrite: run logs are
append-only *by construction*, not by convention.
"""
from __future__ import annotations

import json
import os
import pathlib
from typing import Iterable


class WouldOverwrite(FileExistsError):
    """A run log already exists at this path; runs are never rewritten."""


def _replace_into(path: pathlib.Path, write) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        write(handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def atomic_write_text(path: pathlib.Path, text: str) -> None:
    _replace_into(path, lambda h: h.write(text))


def atomic_write_json(path: pathlib.Path, data, *, indent: int | None = 1) -> None:
    _replace_into(path, lambda h: json.dump(
        data, h, ensure_ascii=False, indent=indent, sort_keys=False))


def write_jsonl_new(path: pathlib.Path, lines: Iterable[dict]) -> int:
    """Write a brand-new JSON Lines file; raise if the path already exists.

    Returns the number of lines written. The existence check happens before
    the temp file is created and ``os.replace`` would clobber a file that
    appeared in between only on a race within the same process — which the
    writer concurrency group prevents in CI and is not worth defending
    against on a laptop.
    """
    path = pathlib.Path(path)
    if path.exists():
        raise WouldOverwrite(str(path))
    count = 0

    def write(handle):
        nonlocal count
        for line in lines:
            # Compact separators: the line must start with {"ck":"…" so the
            # seen-key rebuild can regex the prefix (and the files are ~15% smaller).
            handle.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1

    _replace_into(path, write)
    return count
