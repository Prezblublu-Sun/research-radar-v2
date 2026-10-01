"""Health verdicts (ported semantics from v1 ADR-0030): block only when the
data is missing or largely unscored."""
from __future__ import annotations

from radar.pipeline.health import evaluate


def test_success_is_healthy():
    v = evaluate({"run_status": "success", "quality_flags": []})
    assert v.ok and v.warnings == []


def test_single_source_failure_is_a_warning_not_a_block():
    v = evaluate({"run_status": "partial_success", "quality_flags": ["arxiv_failed"],
                  "counts": {"scorer_failed": 0, "priority_counts": {"Medium": 13}}})
    assert v.ok
    assert any("partial_success" in w for w in v.warnings)
    assert any("arxiv_failed" in w for w in v.warnings)


def test_truncation_and_low_counts_are_warnings():
    v = evaluate({"run_status": "success",
                  "quality_flags": ["low_fetch_count", "openalex_truncated", "zero_high_medium"]})
    assert v.ok and len(v.warnings) == 3


def test_failed_status_and_fetched_zero_block():
    v = evaluate({"run_status": "failed", "quality_flags": ["fetched_zero"]})
    assert not v.ok and len(v.blocking) == 2


def test_missing_status_blocks():
    assert not evaluate({}).ok


def test_one_scorer_failure_among_many_is_a_warning():
    v = evaluate({"run_status": "success", "quality_flags": ["scorer_failed"],
                  "counts": {"scorer_failed": 1, "priority_counts": {"High": 2, "Medium": 10, "Low": 30}}})
    assert v.ok and v.warnings == ["1 papers failed scoring"]


def test_scorer_failing_on_most_papers_blocks():
    v = evaluate({"run_status": "success", "quality_flags": ["scorer_failed"],
                  "counts": {"scorer_failed": 40, "priority_counts": {"Medium": 3, "Low": 5}}})
    assert not v.ok


def test_scorer_failing_on_every_paper_blocks():
    v = evaluate({"run_status": "success", "quality_flags": ["scorer_failed"],
                  "counts": {"scorer_failed": 7, "priority_counts": {}}})
    assert not v.ok
