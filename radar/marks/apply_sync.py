"""Commit a marks payload pasted into a GitHub issue.

The workflow has already checked that the issue carries the ``marks-sync``
label and was opened by the repository owner. The body still reaches this
code as untrusted text: everything goes through ``validate_payload`` before
a byte is written, and the body comes from an environment variable or a
file, never from a shell interpolation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from radar.paths import DataRoot
from radar.store import marks as _marks


@dataclass
class SyncResult:
    ok: bool
    message: str
    device: str = ""
    written: str = ""


def apply(body: str, root: DataRoot, *, dry_run: bool = False) -> SyncResult:
    try:
        payload = _marks.validate_payload(_marks.extract_payload(body))
    except _marks.MarksPayloadError as error:
        return SyncResult(ok=False, message=f"marks sync rejected: {error}")
    counts: dict[str, int] = {}
    for mark in payload["marks"].values():
        key = mark["state"] or "note-only"
        counts[key] = counts.get(key, 0) + 1
    notes = sum(1 for mark in payload["marks"].values() if mark["note"])
    if dry_run:
        return SyncResult(ok=True, device=payload["device"],
                          message=f"marks sync OK (dry run): device {payload['device']}, "
                                  f"{len(payload['marks'])} mark(s) {counts}, {notes} with notes")
    path = _marks.write_device(root, payload)
    merged = _marks.load_all(root)
    message = (f"已同步设备 `{payload['device']}`：{len(payload['marks'])} 条标记"
               f"（{json.dumps(counts, ensure_ascii=False)}，其中 {notes} 条带笔记），写入 `data/marks/{path.name}`。"
               f"\n\n合并全部设备后共 {len(merged)} 条标记，待阅读 {len(_marks.by_state(merged, 'to-read'))} 条。")
    return SyncResult(ok=True, message=message, device=payload["device"], written=str(path))
