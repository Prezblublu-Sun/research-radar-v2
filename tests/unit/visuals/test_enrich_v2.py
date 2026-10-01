"""The v2 seams of the enricher: candidates from the corpus, cache reuse by
TTL, error preservation, and the append-only registry."""
from __future__ import annotations

import datetime as dt

from radar.core.atomic import write_jsonl_new
from radar.store import visuals as store
from radar.visuals import enrich as ev

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.timezone.utc)


def _paper(key, priority="High", first_seen="2026-10-01T00:00:00Z", **extra):
    kind, value = key.split(":", 1)
    paper = {"doi" if kind == "doi" else "arxiv_id": value, "title": key, "llm": {"priority": priority},
             "first_seen_at": first_seen, "date": "2026-09-01"}
    paper.update(extra)
    return paper


def _available(checked_at="2026-09-01T00:00:00Z", **extra):
    v = {"status": "available", "checked_at": checked_at, "image_url": "https://arxiv.org/html/2601.00001v1/x1.png",
         "source_url": "https://arxiv.org/abs/2601.00001v1", "license": "CC BY 4.0", "caption": "Figure 1: a mesh.",
         "provider": "arxiv", "selector_version": ev.SELECTOR_VERSION, "width": 640, "height": 480}
    v.update(extra)
    return v


class FakeResolver:
    def __init__(self, answers=None, raise_for=()):
        self.answers, self.raise_for, self.calls = answers or {}, set(raise_for), []

    def resolve(self, paper, *, now=None):
        key = ev.identity_key(paper)
        self.calls.append(key)
        if key in self.raise_for:
            raise RuntimeError("provider down")
        return dict(self.answers.get(key) or ev._blank_visual("not_found", checked_at=ev.iso_z(now), reason="no_supported_public_figure_source"))


def test_candidates_are_newest_discovery_first_then_priority_and_filterable():
    papers = [_paper("doi:10.1/a", "Medium", "2026-10-01T00:00:00Z"), _paper("doi:10.1/b", "High", "2026-10-01T00:00:00Z"),
              _paper("doi:10.1/c", "High", "2026-09-01T00:00:00Z"), _paper("doi:10.1/d", "Low"), {"title": "no identity", "llm": {"priority": "High"}}]
    keys = [k for k, _, _ in ev.candidates_from_corpus(papers, {"High", "Medium"})]
    assert keys == ["doi:10.1/b", "doi:10.1/a", "doi:10.1/c"]
    assert [k for k, _, _ in ev.candidates_from_corpus(papers, {"High"}, identities={"doi:10.1/c"})] == ["doi:10.1/c"]


def test_fresh_cache_is_reused_and_stale_cache_resolved_again():
    papers = [_paper("doi:10.1/fresh"), _paper("doi:10.1/stale"), _paper("doi:10.1/new")]
    registry = {"doi:10.1/fresh": _available(checked_at="2026-09-30T00:00:00Z"),
                "doi:10.1/stale": ev._blank_visual("not_found", checked_at="2026-08-01T00:00:00Z", reason="x")}
    resolver = FakeResolver({"doi:10.1/stale": _available(checked_at=ev.iso_z(NOW))})
    result = ev.enrich(candidates=ev.candidates_from_corpus(papers, {"High"}), registry=registry, resolver=resolver, limit=10, now=NOW)
    assert sorted(resolver.calls) == ["doi:10.1/new", "doi:10.1/stale"]
    assert set(result["changed"]) == {"doi:10.1/stale", "doi:10.1/new"}
    assert result["counts"] == {"available": 1, "not_found": 1} and result["attempted"] == 2


def test_limit_caps_resolutions_and_force_ignores_the_cache():
    papers = [_paper(f"doi:10.1/{n}") for n in range(5)]
    registry = {"doi:10.1/0": _available(checked_at="2026-10-01T00:00:00Z")}
    resolver = FakeResolver()
    result = ev.enrich(candidates=ev.candidates_from_corpus(papers, {"High"}), registry=registry, resolver=resolver, limit=2, now=NOW)
    assert result["attempted"] == 2 and len(resolver.calls) == 2 and "doi:10.1/0" not in resolver.calls
    forced = ev.enrich(candidates=ev.candidates_from_corpus(papers[:1], {"High"}), registry=registry, resolver=FakeResolver(), limit=5, force=True, now=NOW)
    assert forced["attempted"] == 1


def test_a_transient_error_preserves_the_last_known_good_visual():
    papers = [_paper("doi:10.1/a")]
    registry = {"doi:10.1/a": _available(checked_at="2026-01-01T00:00:00Z")}   # older than the 180-day TTL
    result = ev.enrich(candidates=ev.candidates_from_corpus(papers, {"High"}), registry=registry,
                       resolver=FakeResolver(raise_for={"doi:10.1/a"}), limit=5, now=NOW)
    visual = result["changed"]["doi:10.1/a"]
    assert visual["status"] == "available" and visual["image_url"].endswith("x1.png")
    assert visual["selector_error_at"] == ev.iso_z(NOW) and "provider down" in visual["selector_error_reason"]
    assert result["counts"] == {"preserved_available": 1}


def test_a_fresh_alias_under_another_doi_case_is_copied_to_the_exact_key():
    papers = [_paper("doi:10.1/ABC")]
    registry = {"doi:10.1/abc": _available(checked_at="2026-09-30T00:00:00Z")}
    resolver = FakeResolver()
    result = ev.enrich(candidates=ev.candidates_from_corpus(papers, {"High"}), registry=registry, resolver=resolver, limit=5, now=NOW)
    assert resolver.calls == [] and result["changed"] == {"doi:10.1/ABC": registry["doi:10.1/abc"]}


def test_store_appends_and_latest_checked_at_wins(data_root):
    a = store.append_visuals_run(data_root, "2026-10-01T120000Z", {"doi:10.1/a": _available(checked_at="2026-10-01T00:00:00Z"),
                                                                    "doi:10.1/b": ev._blank_visual("not_found", checked_at="2026-10-01T00:00:00Z", reason="x")})
    assert a == data_root.visuals / "2026" / "2026-10-01T120000Z.jsonl"
    assert a.read_text(encoding="utf-8").splitlines()[1].startswith('{"identity_key":"doi:10.1/a"')
    store.append_visuals_run(data_root, "2026-10-05T120000Z", {"doi:10.1/b": _available(checked_at="2026-10-05T00:00:00Z")})
    # an older observation imported later never beats a newer one
    write_jsonl_new(data_root.visuals / "import-v1" / "index.jsonl",
                    [{"kind": "visuals", "run_id": "import-v1"}, {"identity_key": "doi:10.1/b", "status": "blocked", "checked_at": "2026-08-01T00:00:00Z"}])
    latest = store.latest_visuals(data_root)
    assert latest["doi:10.1/a"]["status"] == "available" and latest["doi:10.1/b"]["status"] == "available"
    assert latest["doi:10.1/b"]["checked_at"] == "2026-10-05T00:00:00Z" and "identity_key" not in latest["doi:10.1/b"]
