"""Decide whether a run's data is fit to publish.

Blocks only when the day's data is missing or largely unscored; a single
source outage, a truncated window or a thin day are warnings, because the
data that was committed is good and hiding it helps nobody (v1 ADR-0030).
"""
from __future__ import annotations

from dataclasses import dataclass, field

BLOCKING_FLAGS = {"fetched_zero"}
WARN_SUFFIXES = ("_failed", "_truncated", "_returned_zero")
WARN_FLAGS = {"low_fetch_count", "zero_routed", "zero_high_medium",
              "scorer_budget_exhausted"}


@dataclass
class Verdict:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking


def evaluate(header: dict) -> Verdict:
    verdict = Verdict()
    status = header.get("run_status")
    flags = list(header.get("quality_flags") or [])
    counts = header.get("counts") or {}

    if status not in ("success", "partial_success"):
        verdict.blocking.append(f"run_status is {status!r}")
    if status == "partial_success":
        verdict.warnings.append("partial_success: at least one source failed")

    for flag in flags:
        if flag in BLOCKING_FLAGS:
            verdict.blocking.append(f"quality flag {flag}")
        elif flag == "scorer_failed":
            failed = int(counts.get("scorer_failed") or 0)
            scored = sum(int(v) for v in (counts.get("priority_counts") or {}).values())
            if failed >= max(scored, 1):
                verdict.blocking.append(f"{failed} papers failed scoring against {scored} scored")
            else:
                verdict.warnings.append(f"{failed} papers failed scoring")
        elif flag in WARN_FLAGS or flag.endswith(WARN_SUFFIXES):
            verdict.warnings.append(f"quality flag {flag}")
    return verdict
