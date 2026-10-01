"""The router must still catch the papers the reader actually chose to read.

``tests/fixtures/eval/picks.jsonl`` holds the 121 papers marked 待读 in the
v1 radar through 2026-10-01 with their v1 abstracts. ``expected_direction``
is the primary direction the v2 router assigned when the fixture was
frozen, so a later config edit shows up as disagreement, not silence.
"""
from __future__ import annotations

import pathlib

from radar.eval import picks

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "eval" / "picks.jsonl"


def test_router_recall_on_reader_picks(cfg):
    fixture = picks.load_fixture(FIXTURE)
    assert len(fixture) >= 120
    report = picks.route_report(fixture, cfg)
    assert report["recall"] >= 0.95, f"unrouted picks: {report['misses']}"
    assert report["agreement"] >= 0.70, report["table"]


def test_every_direction_is_represented_in_the_picks(cfg):
    fixture = picks.load_fixture(FIXTURE)
    primaries = {r["expected_direction"] for r in fixture}
    missing = set(cfg.keys) - primaries
    assert not missing, f"no pick routes primarily to {sorted(missing)}"
