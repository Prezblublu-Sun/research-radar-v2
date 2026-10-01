"""Parsing the reader's pasted 待读 list and resolving it to records."""
from __future__ import annotations

from radar.eval import picks

_MD = """# 待读清单

- [Neural operators for stress fields](https://doi.org/10.1016/J.CMA.2026.1) — 2026-09-24 · 待阅读
- A plain title with no link at all — 2026-05-05 · 待阅读
- [arxiv:2604.03788v3](https://arxiv.org/abs/2604.03788v3) — 待阅读
- [Neural operators for stress fields](https://doi.org/10.1016/j.cma.2026.1) — 2026-09-25 · 待阅读
- doi:10.5555/bare — 待阅读
"""


def test_parse_extracts_identity_date_and_collapses_duplicates():
    out = picks.parse_picks_md(_MD)
    assert [p.title for p in out] == ["Neural operators for stress fields", "A plain title with no link at all",
                                       "arxiv:2604.03788v3", "doi:10.5555/bare"]
    assert out[0].identity_key == "doi:10.1016/J.CMA.2026.1"
    assert out[0].ck == "doi:10.1016/j.cma.2026.1"
    assert out[0].date == "2026-09-24"
    assert out[1].identity_key == "" and out[1].ck == ""
    assert out[2].ck == "arxiv:2604.03788" and out[2].date == ""
    assert out[3].ck == "doi:10.5555/bare"


def test_resolve_joins_by_ck_then_title_and_routes(cfg):
    parsed = picks.parse_picks_md(_MD)
    records = [
        {"doi": "10.1016/j.cma.2026.1", "title": "Neural operators for stress fields",
         "abstract": "A neural operator predicts finite element stress fields on varying geometries.",
         "direction": "fea_surrogate", "llm": {"priority": "High", "relevance_level": "Direct"}},
        {"title": "A plain title with no link at all", "abstract": "femoral stem stress shielding finite element",
         "arxiv_id": "2601.00001v1", "source": "arxiv"},
    ]
    resolved, missing = picks.resolve_picks(parsed, records, cfg)
    assert [r["title"] for r in resolved] == ["Neural operators for stress fields", "A plain title with no link at all"]
    assert resolved[0]["v1_priority"] == "High" and resolved[0]["expected_direction"] in cfg.keys
    assert resolved[1]["identity_key"] == "arxiv:2601.00001v1"
    assert [p.title for p in missing] == ["arxiv:2604.03788v3", "doi:10.5555/bare"]


def test_route_report_counts_misses(cfg):
    fixture = [{"title": "On the weather in London", "abstract": "rain", "expected_direction": None},
               {"title": "Neural operator surrogate", "abstract": "neural operators for finite element stress",
                "expected_direction": "geo_operator"}]
    report = picks.route_report(fixture, cfg)
    assert report["n"] == 2 and report["routed"] == 1
    assert report["misses"] == ["On the weather in London"]
    assert 0 < report["recall"] < 1
