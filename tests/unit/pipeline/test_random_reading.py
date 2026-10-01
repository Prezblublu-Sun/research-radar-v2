"""Random reading (v1 ADR-0035): isolated from the corpus, fail-soft, seeded.

Ported from the v1 suite; the storage assertions now target the append-only
stream under data/random_reading/.
"""
from __future__ import annotations

import json

from radar.pipeline import random_reading as rr
from radar.store import runs as _runs


def _high(venue="Computational Mechanics", venue_id="S147854436", venue_type="journal", **extra):
    paper = {"llm": {"priority": "High"}, "venue": venue, "venue_id": venue_id,
             "venue_type": venue_type, "venue_issn_l": "0178-7675", "direction": "stem_biomech",
             "title": "Seed paper", "doi": "10.1/seed", "source": "openalex"}
    paper.update(extra)
    return paper


class FakeOpenAlex:
    """A journal with ``count`` works, addressable by 1-based position."""
    PAGE_LIMIT = 10_000

    def __init__(self, counts: dict, *, fail_count=False, fail_draw=False, doi_at=None, titles=None):
        self.counts, self.fail_count, self.fail_draw = counts, fail_count, fail_draw
        self.doi_at = doi_at or (lambda sid, pos: f"10.9/{sid}-{pos}")
        self.titles = titles or {}
        self.calls = []

    def journal_month_count(self, source_id, from_date, to_date):
        self.calls.append(("count", source_id, from_date, to_date))
        if self.fail_count:
            raise RuntimeError("OpenAlex is down")
        return self.counts.get(source_id, 0)

    def journal_work_at(self, source_id, from_date, to_date, position):
        self.calls.append(("draw", source_id, position))
        if self.fail_draw:
            raise RuntimeError("OpenAlex is down")
        if position > self.counts.get(source_id, 0):
            return None
        return {"source": "openalex", "id": f"https://openalex.org/W{position}",
                "doi": self.doi_at(source_id, position),
                "title": self.titles.get(position, f"Work {position}"), "abstract": "",
                "authors": [], "venue": "J", "venue_id": source_id, "venue_type": "journal",
                "date": "2026-09-10", "year": 2026}


def _journal(**extra):
    j = {"venue_id": "S1", "venue": "J", "issn_l": "", "direction": "stem_biomech",
         "seed_title": "t", "seed_identity_key": "doi:10.1/seed", "seed_count": 1}
    j.update(extra)
    return j


# --- which journals qualify -------------------------------------------------

def test_only_journals_behind_a_high_paper_qualify():
    scored = [_high(), _high(venue="Nature Communications", venue_id="S64187185"),
              _high(venue="Nature Communications", venue_id="S64187185"),
              _high(venue="arXiv", venue_id="S4306400194", venue_type="repository"),
              _high(venue="No id", venue_id=""),
              {"llm": {"priority": "Medium"}, "venue": "Other", "venue_id": "S9", "venue_type": "journal"}]
    journals = rr.journals_of_high_papers(scored)
    assert [j["venue_id"] for j in journals] == ["S147854436", "S64187185"]
    assert journals[1]["seed_count"] == 2
    assert journals[0]["seed_identity_key"] == "doi:10.1/seed"
    assert journals[0]["direction"] == "stem_biomech"


def test_journal_list_is_capped():
    scored = [_high(venue=f"J{n}", venue_id=f"S{n}") for n in range(20)]
    assert len(rr.journals_of_high_papers(scored)) == rr.MAX_JOURNALS


def test_month_window_is_the_whole_calendar_month():
    assert rr.month_window("2026-09-25") == ("2026-09-01", "2026-09-30")
    assert rr.month_window("2024-02-29") == ("2024-02-01", "2024-02-29")


def test_allocation_is_inverse_to_journal_size():
    assert rr.picks_for_volume(0) == 0
    assert rr.picks_for_volume(40) == rr.SPECIALIST_PICKS == 5
    assert rr.picks_for_volume(200) == rr.SPECIALIST_PICKS
    assert rr.picks_for_volume(201) == rr.MEGAJOURNAL_PICKS == 2


# --- the draw ------------------------------------------------------------------

def test_specialist_journal_gives_five_and_megajournal_two():
    fake = FakeOpenAlex({"S1": 40, "S2": 3000})
    picks, report = rr.sample_journal(_journal(), "2026-09-25", set(), fake)
    assert len(picks) == 5 and report["target_picks"] == 5 and report["month_works"] == 40
    assert all(p["random_reading"]["venue_id"] == "S1" for p in picks)
    assert sorted(report["positions"]) == sorted(p["random_reading"]["position"] for p in picks)
    picks2, report2 = rr.sample_journal(_journal(venue_id="S2"), "2026-09-25", set(), fake)
    assert len(picks2) == 2 and report2["target_picks"] == 2


def test_draw_is_seeded_by_day_and_journal():
    a, _ = rr.sample_journal(_journal(), "2026-09-25", set(), FakeOpenAlex({"S1": 500}))
    b, _ = rr.sample_journal(_journal(), "2026-09-25", set(), FakeOpenAlex({"S1": 500}))
    c, _ = rr.sample_journal(_journal(), "2026-09-26", set(), FakeOpenAlex({"S1": 500}))
    assert [p["doi"] for p in a] == [p["doi"] for p in b]
    assert [p["doi"] for p in a] != [p["doi"] for p in c]


def test_known_papers_are_skipped_within_the_headroom():
    fake = FakeOpenAlex({"S1": 12})
    known = {f"doi:10.9/s1-{n}" for n in range(1, 13)} - {"doi:10.9/s1-3", "doi:10.9/s1-7"}
    picks, report = rr.sample_journal(_journal(), "2026-09-25", known, fake)
    assert sorted(p["doi"] for p in picks) == ["10.9/S1-3", "10.9/S1-7"]
    assert report["skipped_known"] == 10
    assert "doi:10.9/s1-3" in known  # a pick joins the known set at once


def test_top_up_continues_the_same_ordering():
    fake = FakeOpenAlex({"S1": 60})
    full, _ = rr.sample_journal(_journal(), "2026-09-25", set(), fake)
    partial, _ = rr.sample_journal(_journal(), "2026-09-25", set(), FakeOpenAlex({"S1": 60}), have=2)
    assert [p["doi"] for p in partial] == [p["doi"] for p in full[:3]]


def test_count_failure_and_draw_failure_are_reported_not_raised():
    picks, report = rr.sample_journal(_journal(), "2026-09-25", set(), FakeOpenAlex({"S1": 40}, fail_count=True))
    assert picks == [] and report["error"].startswith("count failed")
    picks, report = rr.sample_journal(_journal(), "2026-09-25", set(), FakeOpenAlex({"S1": 40}, fail_draw=True))
    assert picks == [] and report["error"].startswith("draw failed")


def test_fetcher_without_journal_methods_is_an_error_not_a_crash():
    class Bare:
        pass
    picks, report = rr.sample_journal(_journal(), "2026-09-25", set(), Bare())
    assert picks == [] and "AttributeError" in report["error"]


# --- collect -----------------------------------------------------------------------

def test_collect_routes_picks_and_inherits_the_seed_direction(cfg):
    titles = {n: "Weather on Mars" for n in range(1, 41)}
    titles[5] = "Neural operators for finite element stress fields"
    fake = FakeOpenAlex({"S147854436": 40}, titles=titles)
    picks, report = rr.collect([_high()], set(), "2026-09-25", cfg, fetcher=fake)
    assert report["papers"] == len(picks) == 5 and report["errors"] == 0
    for paper in picks:
        assert paper["direction"] in cfg.keys
        if paper["title"] == "Weather on Mars":
            assert paper["direction"] == "stem_biomech"
            assert paper["routing_reason"] == "inherited from the seed High paper"


def test_collect_with_no_high_papers_draws_nothing():
    fake = FakeOpenAlex({"S1": 40})
    picks, report = rr.collect([{"llm": {"priority": "Medium"}}], set(), "2026-09-25", None, fetcher=fake)
    assert picks == [] and report == {"journals": [], "papers": 0, "errors": 0}
    assert fake.calls == []


# --- storage ------------------------------------------------------------------------

def test_persist_writes_header_then_records_with_random_provenance(make_ctx, stub_fetchers, data_root):
    ctx = make_ctx(stub_fetchers())
    fake = FakeOpenAlex({"S147854436": 40})
    picks, report = rr.collect([_high()], set(), ctx.today_iso, ctx.config, fetcher=fake)
    header = rr.build_header(ctx, picks, report["journals"])
    path = rr.persist_random(ctx, header, picks)
    assert path == data_root.random_reading / "2026" / f"{ctx.run_id}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    head = json.loads(lines[0])
    assert head["kind"] == "random_reading" and head["counts"] == {"journals": 1, "papers": 5, "errors": 0}
    assert head["journals"][0]["seed_identity_key"] == "doi:10.1/seed"
    for line in lines[1:]:
        assert line.startswith('{"ck":"')
        rec = json.loads(line)
        assert rec["provenance"]["origin"] == "random" and rec["provenance"]["venue_id"] == "S147854436"
        assert rec["random_reading"]["position"] in head["journals"][0]["positions"]
    assert rr.drawn_keys(data_root) == _runs.scan_keys(path) and len(rr.drawn_keys(data_root)) == 5
