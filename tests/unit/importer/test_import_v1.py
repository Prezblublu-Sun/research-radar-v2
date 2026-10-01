"""The v1 import: canonical winners, selection, re-routing, provenance,
month shards, the unrouted audit, visuals and aliases."""
from __future__ import annotations

import json

from radar.importer import v1 as imp
from radar.store import corpus as _corpus
from radar.store import runs as _runs
from radar.store import visuals as _vstore


def _v1(doi="", arxiv_id="", *, title, abstract, priority, direction="fea_surrogate", first_seen="2026-09-02T07:00:00Z", **extra):
    paper = {"source": "openalex" if doi else "arxiv", "id": doi or arxiv_id, "doi": doi, "arxiv_id": arxiv_id,
             "title": title, "abstract": abstract, "authors": ["A"], "venue": "V", "date": "2026-09-01",
             "direction": direction, "direction_name": "FEA & Surrogate", "directions": [direction],
             "routing_matches": {direction: ["x"]}, "first_seen_at": first_seen, "scorer_version": "v3",
             "schema_version": "v2", "concepts": [{"id": "C1"}],
             "llm": {"priority": priority, "relevance_level": "Direct", "tags": ["t"], "summary_zh": {"motivation": "m"}}}
    paper.update(extra)
    return paper


def _old_repo(tmp_path):
    old = tmp_path / "old"
    daily = old / "data" / "daily"
    daily.mkdir(parents=True)
    neural = "Neural operators predict finite element stress fields on varying geometries."
    stem = "Finite element analysis of femoral stem stress shielding and bone remodelling."
    (daily / "2026-09-01.json").write_text(json.dumps({"schema_version": "v2", "date": "2026-09-01", "papers": [
        _v1("10.1/HIGH", title="High neural operator", abstract=neural, priority="High"),
        _v1("10.1/medium", title="Medium stem", abstract=stem, priority="Medium", direction="hip_implant"),
        _v1("10.1/low-pick", title="Low but picked", abstract="A paper about weather.", priority="Low"),
        _v1("10.1/low", title="Low not picked", abstract=neural, priority="Low"),
        _v1("10.1/unrouted", title="Bioprinting high", abstract="AI bioprinting of bioinks.", priority="High", direction="ai_bioprinting"),
        _v1("10.5281/zenodo.22057604", title="Zenodo version", abstract=neural, priority="High"),
        {"source": "pubmed", "title": "no identity", "llm": {"priority": "High"}},
    ]}), encoding="utf-8")
    (daily / "2026-08-15.json").write_text(json.dumps({"schema_version": "v2", "date": "2026-08-15", "papers": [
        _v1("10.1/high", title="High neural operator (earlier observation)", abstract=neural, priority="High",
            first_seen="2026-08-16T07:00:00Z"),
        _v1("10.5281/zenodo.22057603", title="Zenodo concept", abstract=neural, priority="Medium"),
    ]}), encoding="utf-8")
    (daily / "2026-07-01.SKIPPED.json").write_text("{}", encoding="utf-8")
    (old / "data" / "doi_aliases.json").write_text(json.dumps({"schema_version": 1, "zenodo": {
        "10.5281/zenodo.22057604": "10.5281/zenodo.22057603", "10.5281/zenodo.22057603": "10.5281/zenodo.22057603"}}), encoding="utf-8")
    (old / "data" / "visuals").mkdir()
    (old / "data" / "visuals" / "index.json").write_text(json.dumps({"schema_version": "v1", "records": {
        "doi:10.1/HIGH": {"status": "available", "checked_at": "2026-09-05T00:00:00Z", "provider": "arxiv"},
        "doi:10.1/x": {"status": "not_found", "checked_at": "2026-09-05T00:00:00Z"}}}), encoding="utf-8")
    picks = tmp_path / "picks.jsonl"
    picks.write_text(json.dumps({"ck": "doi:10.1/low-pick", "title": "Low but picked"}) + "\n", encoding="utf-8")
    return old, picks


def test_import_selects_reroutes_and_writes_month_shards(tmp_path, data_root, cfg, repo_root):
    old, picks = _old_repo(tmp_path)
    report = imp.run_import(old, data_root, cfg, prompts_dir=repo_root / "prompts", priorities={"High", "Medium"},
                            picks_path=picks, log=lambda *_: None)
    assert report.raw == 9 and report.unique == 6          # case-variant DOI + Zenodo pair merged; no-identity dropped
    assert report.selected == 5 and report.by_v1_priority == {"High": 2, "Medium": 2, "Low": 1}
    assert report.routed == 3 and report.unrouted == 2 and report.picks_kept_unrouted == 1
    assert report.unrouted_by_v1_direction == {"ai_bioprinting": 1, "fea_surrogate": 1}
    assert len(report.shards) == 2 and report.visuals == 2 and report.aliases == 2

    aug = list(_runs.iter_records(data_root.runs / "import-v1" / "2026-08.jsonl"))
    sep = list(_runs.iter_records(data_root.runs / "import-v1" / "2026-09.jsonl"))
    assert [r["title"] for r in aug] == ["High neural operator (earlier observation)", "Zenodo concept"]
    high = aug[0]
    assert high["ck"] == "doi:10.1/high" and high["identity_key"] == "doi:10.1/high"   # earliest observation wins
    assert high["run_type"] == "import_v1" and high["scored_at"] == "2026-08-16T07:00:00Z"
    assert high["scorer_version"] == "v3" and high["prompt_sha"].startswith("sha256:")
    assert high["llm"]["priority"] == "High" and "priority_boosted" not in high["llm"]
    assert high["direction"] == "geo_operator" and high["provenance"]["v1_direction"] == "fea_surrogate"
    assert high["provenance"]["origin"] == "import_v1" and "concepts" not in high
    assert {r["title"] for r in sep} == {"Medium stem", "Low but picked"}
    pick = next(r for r in sep if r["title"] == "Low but picked")
    assert pick["direction"] is None and pick["routing_reason"] == "eval pick"
    header = _runs.read_header(data_root.runs / "import-v1" / "2026-09.jsonl")
    assert header["run_type"] == "import_v1" and header["config"]["scorer_prompt_file"] == "scorer_v3.txt"

    audit = [json.loads(l) for l in (data_root.eval / "import-v1-unrouted.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [a["title"] for a in audit] == ["Bioprinting high"] and audit[0]["v1_direction"] == "ai_bioprinting"
    assert (data_root.eval / "picks.jsonl").exists()
    assert _vstore.latest_visuals(data_root)["doi:10.1/HIGH"]["status"] == "available"
    assert json.loads((data_root.aliases / "zenodo.json").read_text(encoding="utf-8"))["zenodo"]["10.5281/zenodo.22057604"] == "10.5281/zenodo.22057603"


def test_imported_runs_are_never_the_newest_and_never_in_the_workbench(tmp_path, data_root, cfg, repo_root, make_ctx, stub_fetchers, sample_records):
    from radar.pipeline import daily
    from radar.pipeline.scorer import ReplayScorer
    from radar.ops import alerts
    from radar.site import views
    old, picks = _old_repo(tmp_path)
    imp.run_import(old, data_root, cfg, prompts_dir=repo_root / "prompts", priorities={"High", "Medium"}, picks_path=picks, log=lambda *_: None)
    by_source = {"arxiv": [], "openalex": [], "pubmed": []}
    for r in sample_records:
        by_source[r["source"]].append(r)
    report = daily.run_daily(make_ctx(stub_fetchers(**by_source), scorer=ReplayScorer({}, default={"priority": "High"})), log=lambda *_: None)
    c = _corpus.load_corpus(data_root)
    assert c.headers[0]["run_id"] == report.header["run_id"]           # the daily run, not import-v1-2026-09
    assert alerts.recent_headers(data_root)[0]["run_id"] == report.header["run_id"]
    runs = views.run_shards(tmp_path / "site", c, cfg)
    assert [r["run_id"] for r in runs] == [report.header["run_id"]]
    assert c.stats.unique_total == len(report.papers) + 4


def test_dry_run_writes_nothing(tmp_path, data_root, cfg, repo_root):
    old, picks = _old_repo(tmp_path)
    report = imp.run_import(old, data_root, cfg, prompts_dir=repo_root / "prompts", priorities={"High", "Medium"},
                            picks_path=picks, dry_run=True, log=lambda *_: None)
    assert report.selected == 5 and report.shards == [] and not data_root.root.exists()
