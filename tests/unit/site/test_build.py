"""End-to-end site build from real run files produced by the pipeline."""
from __future__ import annotations

import json
import re

import pytest

from radar.pipeline import daily
from radar.pipeline.scorer import ReplayScorer
from radar.site import assets, build


@pytest.fixture
def built(tmp_path, make_ctx, stub_fetchers, sample_records, data_root, cfg):
    by_source = {"arxiv": [], "openalex": [], "pubmed": []}
    for r in sample_records:
        by_source[r["source"]].append(r)
    answers = {}
    for i, r in enumerate(sample_records):
        answers[r.get("doi") or ""] = {"priority": ["High", "Medium", "Low", "Exclude"][i % 4],
                                       "relevance_level": "Direct", "tags": ["t"],
                                       "summary_zh": {"motivation": "动机", "method": "方法"},
                                       "relevance_to_user": "相关", "key_terms": [{"en": "stem", "zh": "柄"}]}
    ctx = make_ctx(stub_fetchers(**by_source), scorer=ReplayScorer(answers, default={"priority": "Low"}))
    report = daily.run_daily(ctx, log=lambda *_: None)
    assert report.ok
    out = tmp_path / "_site"
    site = build.build_site(data_root, out, cfg, repo="owner/repo", log=lambda *_: None)
    return site, out, report


def test_build_produces_shells_shards_and_bundles(built):
    site, out, report = built
    assert site.ok, site.problems
    for name in ("index.html", "queue.html", "search.html", "reading.html", "random-reading.html",
                 "library.html", "status.html", "archive.html", "site-manifest.json", ".nojekyll",
                 "queue-manifest.json", "search-index-manifest.json", "high-priority.html"):
        assert (out / name).exists(), name
    for bundle in assets.BUNDLES:
        assert (out / bundle).exists(), bundle
    assert site.days == len({(p.get("date") or "")[:10] for p in report.papers})
    manifest = json.loads((out / "site-manifest.json").read_text(encoding="utf-8"))
    assert manifest["repo"] == "owner/repo" and manifest["corpus"]["unique_total"] == len(report.papers)
    assert manifest["latest_run"]["run_id"] == report.header["run_id"]


def test_day_shards_match_the_v1_contract(built):
    site, out, report = built
    date = max((p.get("date") or "")[:10] for p in report.papers)
    manifest = json.loads((out / "data" / "day" / date / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["page_pattern"] == "page-{page}.json" and manifest["page_size"] == 20
    page = json.loads((out / "data" / "day" / date / f"page-1.json").read_text(encoding="utf-8"))
    assert page["revision"] == manifest["revision"] and page["papers"]
    record = page["papers"][0]
    for key in ("identity_key", "anchor", "title", "direction_color", "priority", "priority_label",
                "summary_zh", "key_terms", "tags", "scored_by"):
        assert key in record, key
    assert record["scored_by"] == {"prompt": "replay", "origin": "daily"}
    assert manifest["anchor_pages"][record["anchor"]] == 1
    html = (out / f"{date}.html").read_text(encoding="utf-8")
    assert f'data-date="{date}"' in html and 'id="rui-priority-filter"' in html and 'id="rui-marks-filter"' in html
    assert 'href="archive.html" aria-current="page"' in html   # the nav knows which page it is on


def test_workbench_run_shards_are_capped_and_grouped_by_first_run(built):
    site, out, report = built
    run_id = report.header["run_id"]
    payload = json.loads((out / "data" / "run" / f"{run_id}.json").read_text(encoding="utf-8"))
    assert payload["total"] == len(report.papers) and payload["embedded"] <= 150
    priorities = [r["priority"] for r in payload["records"]]
    order = {"High": 0, "Medium": 1, "Unscored": 2, "Low": 3, "Exclude": 4}
    assert priorities == sorted(priorities, key=order.get)
    html = (out / "index.html").read_text(encoding="utf-8")
    assert f'data-run-id="{run_id}"' in html and "radar-workbench.js?v=" in html
    assert 'id="rui-marks-filter"' in html and 'id="rui-priority-filter"' not in html
    assert '<article class="paper"' not in html   # cards are never embedded


def test_queue_and_search_shards(built):
    site, out, report = built
    queue = json.loads((out / "queue-manifest.json").read_text(encoding="utf-8"))
    highs = [p for p in report.papers if p["llm"].get("priority") == "High"]
    assert queue["priorities"]["High"]["total"] == len(highs)
    year = next(iter(queue["priorities"]["High"]["years"]))
    records = json.loads((out / f"queue-high-{year}.json").read_text(encoding="utf-8"))
    assert records and all(r["priority"] == "High" for r in records)
    search = json.loads((out / "search-index-manifest.json").read_text(encoding="utf-8"))
    assert search["total"] == len(report.papers)
    deep = json.loads((out / f"search-deep-{search['years'][0]}.json").read_text(encoding="utf-8"))
    assert deep and "deep_blob" in deep[0]


def test_every_page_references_versioned_bundles_only(built):
    site, out, report = built
    for page in out.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for name in assets.BUNDLES:
            assert f'src="{name}"' not in html and f'href="{name}"' not in html, (page.name, name)
        assert "radar-ui.css?v=" in html, page.name


def test_filter_bars_appear_exactly_where_filters_should_apply(built):
    site, out, report = built
    with_marks = {p.name for p in out.glob("*.html") if 'id="rui-marks-filter"' in p.read_text(encoding="utf-8")}
    dated = {p.name for p in out.glob("20*.html")}
    assert with_marks == {"index.html", "queue.html", "random-reading.html"} | dated
    reading = (out / "reading.html").read_text(encoding="utf-8")
    assert 'data-rui-no-filter="1"' in reading and 'id="rui-marks-filter"' not in reading


def test_status_page_lists_the_run(built):
    site, out, report = built
    html = (out / "status.html").read_text(encoding="utf-8")
    assert report.header["run_id"] in html and "scorer_v4.txt" in html


def test_safe_visuals_from_the_stream_reach_the_day_shards(tmp_path, make_ctx, stub_fetchers, sample_records, data_root, cfg):
    from radar.store import visuals as vstore
    by_source = {"arxiv": [], "openalex": [], "pubmed": []}
    for r in sample_records:
        by_source[r["source"]].append(r)
    ctx = make_ctx(stub_fetchers(**by_source), scorer=ReplayScorer({}, default={"priority": "High"}))
    report = daily.run_daily(ctx, log=lambda *_: None)
    from radar.core import identity
    target = next(p for p in report.papers if p.get("source") == "arxiv")
    key = identity.identity_key(target)
    good = {"status": "available", "checked_at": "2026-10-01T00:00:00Z", "provider": "arxiv", "license": "CC BY 4.0",
            "image_url": "https://arxiv.org/html/2601.00001v1/x1.png", "source_url": "https://arxiv.org/abs/2601.00001v1",
            "caption": "Figure 1: the mesh.", "width": 640, "height": 480, "media_type": "image/png"}
    unsafe = dict(good, caption="Figure 2: reproduced with permission from X.")
    vstore.append_visuals_run(data_root, "2026-10-01T130000Z", {key: good, "doi:10.1/other": unsafe})
    out = tmp_path / "_site"
    build.build_site(data_root, out, cfg, log=lambda *_: None)
    date = (target.get("date") or "")[:10]
    page = json.loads((out / "data" / "day" / date / "page-1.json").read_text(encoding="utf-8"))
    card = next(r for r in page["papers"] if r["identity_key"] == key)
    assert card["visual"]["image_url"] == good["image_url"] and card["visual"]["license"] == "CC BY 4.0"
    assert "provider" not in card["visual"] or card["visual"].get("provider") == "mdpi"
    assert all("visual" not in r for r in page["papers"] if r["identity_key"] != key)


def test_validate_catches_a_missing_index(tmp_path):
    assert build.validate(tmp_path) == ["index.html is missing"]


def test_empty_data_still_builds(tmp_path, data_root, cfg):
    site = build.build_site(data_root, tmp_path / "_site", cfg, log=lambda *_: None)
    assert site.ok and site.unique_total == 0 and site.runs == 0
    assert "尚无可展示的最新运行" in (tmp_path / "_site" / "index.html").read_text(encoding="utf-8")
