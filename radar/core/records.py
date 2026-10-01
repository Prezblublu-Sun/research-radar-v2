"""Run ids, timestamps and the record shapes written to data/runs/.

A run log is JSON Lines: the first line is the run header, every later line
is one paper. The per-paper key set is deliberately the v1 one kept flat, so
code ported from v1 reads ``paper["llm"]["priority"]`` unchanged. v2 adds a
handful of keys, the first of which — ``ck`` — is always written first so the
seen-key rebuild can read it off the line prefix without parsing JSON.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, TypedDict

SCHEMA_VERSION = "v3"
RUN_TYPES = ("daily", "backfill", "rescore", "import_v1")
_RUN_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def utc_now_iso(now: dt.datetime | None = None) -> str:
    """Seconds-precision ISO 8601 UTC with a Zulu suffix."""
    return ((now or utc_now()).isoformat(timespec="seconds")
            .replace("+00:00", "Z"))


def new_run_id(now: dt.datetime | None = None) -> str:
    """``2026-10-02T121703Z`` — sorts chronologically, safe in a filename."""
    return (now or utc_now()).strftime("%Y-%m-%dT%H%M%SZ")


def is_run_id(text: str) -> bool:
    return bool(_RUN_ID.match(text or ""))


def infer_date_precision(date_str: str) -> str:
    """``*-01-01`` -> year, ``*-01`` -> month, otherwise day; empty -> year."""
    if not date_str:
        return "year"
    if date_str.endswith("-01-01"):
        return "year"
    if date_str.endswith("-01"):
        return "month"
    return "day"


class RunHeader(TypedDict, total=False):
    kind: str                 # "run"
    schema_version: str
    run_id: str
    run_type: str
    started_at: str
    finished_at: str
    git_commit: str
    run_status: str           # success | partial_success | failed
    quality_flags: list[str]
    config: dict[str, Any]
    window: dict[str, Any]
    sources_used: dict[str, Any]
    source_status: dict[str, Any]
    llm: dict[str, Any]
    counts: dict[str, Any]
    random_reading: dict[str, Any]
    packages: dict[str, str]


# The paper record is the v1 shape plus these; see persist.build_paper_record.
RECORD_ADDED_KEYS = ("ck", "kind", "schema_version", "run_id", "run_type",
                     "identity_key", "first_seen_at", "scored_at",
                     "scorer_version", "prompt_sha", "crossover", "provenance")
# Fetcher fields the pipeline never reads; dropped at persist time.
RECORD_DROPPED_KEYS = ("concepts",)
ABSTRACT_MAX_CHARS = 3000   # all the scorer ever reads
