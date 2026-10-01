"""Synced reading marks: one file per browser at ``data/marks/<device>.json``.

Ported from v1 ``pipeline/marks_store.py`` (ADR-0032/0034). The shape::

    {"schema_version": 1, "device": "dev-1a2b3c4d", "updated_at": "…Z",
     "marks": {"<identity_key>": {"state": "to-read"|"read"|"ignore"|"",
                                   "tags": [...], "at": "…Z", "note": "…",
                                   "title": "…", "date": "…", "direction": "…", "priority": "…"}}}

``state`` is where a paper sits in the triage flow (one at a time); ``tags``
are judgements (any number). Payloads arrive pasted into a public issue or
PUT by a browser, so :func:`validate_payload` is a trust boundary: strict,
size-capped, and it never lets a device name travel outside ``data/marks/``.
These are small snapshot files rewritten in place on purpose — the browser's
single-file PUT autosync depends on that layout.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re

from radar.paths import DataRoot

SCHEMA_VERSION = 1
STATES = {"to-read", "read", "ignore", ""}
INSPIRING_TAG = "有启发"
LEGACY_STATES = {"interesting": ("read", INSPIRING_TAG)}
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")
IDENTITY_RE = re.compile(r"^(doi|arxiv|pmid|openalex|noid):[\x21-\x7e]{1,190}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
MAX_MARKS = 5000
MAX_NOTE = 4000
MAX_FIELD = 500
MAX_TAGS = 24
MAX_TAG = 40


class MarksPayloadError(ValueError):
    """The pasted payload is not a well-formed marks file."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_tags(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MarksPayloadError("tags must be a list")
    if len(value) > MAX_TAGS:
        raise MarksPayloadError(f"{len(value)} tags exceeds the {MAX_TAGS} cap")
    seen: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise MarksPayloadError("a tag must be a string")
        tag = " ".join(item.split())[:MAX_TAG]
        if tag and tag not in seen:
            seen.append(tag)
    return sorted(seen)


def _clean(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise MarksPayloadError(f"expected a string, got {type(value).__name__}")
    return value.replace("\r\n", "\n").strip()[:limit]


def validate_payload(data) -> dict:
    """A normalised payload, or :class:`MarksPayloadError`. Unknown keys are dropped."""
    if not isinstance(data, dict):
        raise MarksPayloadError("payload must be a JSON object")
    if int(data.get("schema_version") or 0) != SCHEMA_VERSION:
        raise MarksPayloadError(f"unsupported schema_version {data.get('schema_version')!r}; expected {SCHEMA_VERSION}")
    raw_device = data.get("device")
    if not isinstance(raw_device, str):
        raise MarksPayloadError("device must be a string")
    device = raw_device.strip().lower()
    if not DEVICE_RE.match(device):
        raise MarksPayloadError(f"device {device!r} must match {DEVICE_RE.pattern}")
    marks_in = data.get("marks")
    if not isinstance(marks_in, dict):
        raise MarksPayloadError("marks must be a JSON object keyed by identity")
    if len(marks_in) > MAX_MARKS:
        raise MarksPayloadError(f"{len(marks_in)} marks exceeds the {MAX_MARKS} cap")

    marks: dict[str, dict] = {}
    for key, value in marks_in.items():
        if not isinstance(key, str) or not IDENTITY_RE.match(key):
            raise MarksPayloadError(f"not an identity key: {key!r}")
        if not isinstance(value, dict):
            raise MarksPayloadError(f"mark {key!r} must be an object")
        state = _clean(value.get("state"), 32)
        tags = _clean_tags(value.get("tags"))
        if state in LEGACY_STATES:
            state, implied = LEGACY_STATES[state]
            if implied not in tags:
                tags = sorted(tags + [implied])
        if state not in STATES:
            raise MarksPayloadError(f"mark {key!r} has unknown state {state!r}")
        at = _clean(value.get("at"), 40)
        if at and not ISO_RE.match(at):
            raise MarksPayloadError(f"mark {key!r} has a non-ISO timestamp {at!r}")
        note = _clean(value.get("note"), MAX_NOTE)
        if not state and not note and not tags and not at:
            continue
        # A cleared mark with a timestamp is a tombstone; it must survive the
        # round trip so the merge can beat an older mark on another device.
        marks[key] = {"state": state, "tags": tags, "at": at, "note": note,
                      "title": _clean(value.get("title"), MAX_FIELD), "date": _clean(value.get("date"), 32),
                      "direction": _clean(value.get("direction"), 64), "priority": _clean(value.get("priority"), 32)}
    return {"schema_version": SCHEMA_VERSION, "device": device,
            "updated_at": _clean(data.get("updated_at"), 40) or utc_now(), "marks": marks}


def extract_payload(text: str) -> dict:
    """The marks JSON out of an issue body: a fenced block or a bare object."""
    if not isinstance(text, str) or not text.strip():
        raise MarksPayloadError("the issue body is empty")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text.strip()
    if not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise MarksPayloadError("no JSON object found in the issue body")
        raw = raw[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise MarksPayloadError(f"payload is not valid JSON: {error}") from error


def device_path(root: DataRoot, device: str) -> pathlib.Path:
    if not DEVICE_RE.match(device):
        raise MarksPayloadError(f"refusing to write device {device!r}")
    return root.marks / f"{device}.json"


def write_device(root: DataRoot, payload: dict) -> pathlib.Path:
    """Persist one validated device payload, byte-identical to the browser's own write."""
    path = device_path(root, payload["device"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def is_tombstone(mark: dict) -> bool:
    return not mark.get("state") and not mark.get("note") and not mark.get("tags")


def load_all(root: DataRoot) -> dict[str, dict]:
    """Every device file merged: newest ``at`` wins, tombstones then dropped."""
    merged: dict[str, dict] = {}
    if not root.marks.is_dir():
        return merged
    for path in sorted(root.marks.glob("*.json")):
        try:
            payload = validate_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
        device = payload["device"]
        for key, mark in payload["marks"].items():
            current = merged.get(key)
            if current is None or (mark.get("at", ""), device) > (current.get("at", ""), current.get("device", "")):
                merged[key] = {**mark, "device": device}
    return {key: mark for key, mark in merged.items() if not is_tombstone(mark)}


def _newest_first(pairs: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    pairs.sort(key=lambda item: (item[1].get("at", ""), item[0]), reverse=True)
    return pairs


def by_state(marks: dict[str, dict], state: str) -> list[tuple[str, dict]]:
    return _newest_first([(k, m) for k, m in marks.items() if m.get("state") == state])


def by_tag(marks: dict[str, dict], tag: str) -> list[tuple[str, dict]]:
    return _newest_first([(k, m) for k, m in marks.items() if tag in (m.get("tags") or [])])


def tag_counts(marks: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for mark in marks.values():
        for tag in mark.get("tags") or []:
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
