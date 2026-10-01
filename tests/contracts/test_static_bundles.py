"""The shipped bundles keep the invariants v1 learned the hard way.

The bundles are ported verbatim (plus one additive chip in radar-card.js),
so these string assertions are the memory of five incidents:

(a) a filter applies only on a page that shows its control;
(b) every active filter has a visible control (orphan tag chips at count 0);
(c) an all-hidden page names the filters and offers 显示全部;
(d) every asset URL carries the bundle's content hash;
(e) applyFilters dispatches radar:filters-applied once, from one place.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

from radar.site import assets

STATIC = assets.STATIC_DIR
UI_JS = (STATIC / "radar-ui.js").read_text(encoding="utf-8")
UI_CSS = (STATIC / "radar-ui.css").read_text(encoding="utf-8")
DAY_JS = (STATIC / "radar-day.js").read_text(encoding="utf-8")
QUEUE_JS = (STATIC / "radar-queue.js").read_text(encoding="utf-8")
CARD_JS = (STATIC / "radar-card.js").read_text(encoding="utf-8")
READING_JS = (STATIC / "radar-reading.js").read_text(encoding="utf-8")
SEARCH_JS = (STATIC / "radar-search.js").read_text(encoding="utf-8")
WORKBENCH_JS = (STATIC / "radar-workbench.js").read_text(encoding="utf-8")
RANDOM_JS = (STATIC / "radar-random.js").read_text(encoding="utf-8")
TEMPLATES = pathlib.Path(assets.__file__).resolve().parent / "templates"


# --- (a) gating -------------------------------------------------------------

def test_a_filter_is_only_applied_where_its_control_is_shown():
    block = UI_JS.split("function applyFilters()")[1].split("function announceFiltersApplied")[0]
    assert 'var gradeBar = document.getElementById("rui-priority-filter");' in block
    assert 'var markBar = document.getElementById("rui-marks-filter");' in block
    assert "var prOk = !gradeBar || prios.indexOf(pr) >= 0;" in block
    assert "var mkOk = !markBar || visible(idk ? markRecord(idk) : null);" in block
    assert 'main[data-rui-no-filter]' in UI_JS
    assert UI_JS.index('main[data-rui-no-filter]') < UI_JS.index('dirOk && prOk && mkOk')


def test_the_filters_macro_is_the_only_emitter_of_the_bars():
    emitters = [p for p in TEMPLATES.rglob("*.j2")
                if 'id="rui-priority-filter"' in p.read_text(encoding="utf-8")
                or 'id="rui-marks-filter"' in p.read_text(encoding="utf-8")]
    assert [p.name for p in emitters] == ["filters.html.j2"]


def test_every_template_that_renders_cards_shows_the_marks_bar_or_opts_out():
    for path in TEMPLATES.glob("*.j2"):
        text = path.read_text(encoding="utf-8")
        if "paper-grid" not in text and "data-run-cards" not in text:
            continue
        assert "filters.marks_bar()" in text or "data-rui-no-filter" in text, path.name


# --- (b)/(c) notice and orphan chips ----------------------------------------------

def test_an_all_hidden_page_offers_a_way_to_see_its_papers():
    block = UI_JS.split("function renderBlockedNotice(total, hidden)")[1].split("function announceFiltersApplied")[0]
    assert 'document.querySelectorAll("#rui-priority-filter, #rui-marks-filter")' in block
    assert "if (!bars.length) return;" in block
    assert "var blocked = total > 0 && hidden >= total;" in block
    assert '"显示全部"' in block and 'reset.addEventListener("click", clearFilters);' in block


def test_the_notice_names_the_filter_that_did_it():
    block = UI_JS.split("function activeFilters()")[1].split("function clearFilters")[0]
    for needle in ('"等级未勾选 "', '"标记未勾选 "', '"只看标签 "',
                   'document.getElementById("rui-priority-filter")', 'document.getElementById("rui-marks-filter")'):
        assert needle in block, needle


def test_a_selected_tag_nobody_carries_still_has_a_chip():
    block = UI_JS.split("function renderTagFilter()")[1].split("function announceFilterChange")[0]
    assert "known.push({ tag: tag, count: 0 });" in block


def test_clearing_filters_resets_every_one_and_is_only_offered():
    block = UI_JS.split("function clearFilters()")[1].split("function mergeMeta")[0]
    for needle in ('dirFilter = "all";', 'lsSet("radar:filter:priority", prios);',
                   'lsSet("radar:filter:marks", MARKS_DEFAULT.slice());', "setTagFilter([]);", 'var prios = ["Unscored"];'):
        assert needle in block, needle
    assert UI_JS.count("clearFilters()") == 1
    for name, source in (("day", DAY_JS), ("workbench", WORKBENCH_JS), ("random", RANDOM_JS)):
        assert "clearFilters" not in source, name
        assert "blocked" not in source.lower() or name == "day", name   # radar-ui draws the notice


# --- (e) one announcement ------------------------------------------------------------

def test_filters_applied_is_announced_from_one_place():
    block = UI_JS.split("function applyFilters()")[1].split("function announceFiltersApplied")[0]
    assert "announceFiltersApplied(cards.length, hidden);" in block
    assert "announceFiltersApplied(cards.length, 0);" in block
    assert UI_JS.count("announceFiltersApplied(") == 3
    assert 'document.addEventListener("radar:filters-applied"' in DAY_JS
    assert DAY_JS.count("var pageTotal") == 1 and "var cardsOnPage = 0;" in DAY_JS


def test_the_migration_never_promotes_the_queue_preference():
    block = UI_JS.split("function migrateMarks()")[1].split("migrateMarks();")[0]
    assert 'lsSet("radar:filter:marks", ["ignore"]);' not in block
    assert "var SCHEMA_NOW = 3;" in UI_JS and "if (done === 2) {" in block


# --- (d) cache busting ----------------------------------------------------------------

@pytest.mark.parametrize("name", assets.BUNDLES)
def test_every_bundle_url_carries_its_content_hash(name):
    version = assets.asset_version(name)
    assert re.fullmatch(r"[0-9a-f]{10}", version), name
    assert assets.asset(name) == f"{name}?v={version}"


def test_a_missing_bundle_degrades_to_an_unversioned_url():
    assert assets.asset_version("not-a-bundle.js") == "" and assets.asset("not-a-bundle.js") == "not-a-bundle.js"


def test_the_search_worker_inherits_the_bundle_version():
    assert 'new Worker("radar-search-worker.js" + assetQuery)' in SEARCH_JS
    assert "document.currentScript && document.currentScript.src" in SEARCH_JS


def test_templates_only_reference_assets_through_the_helper():
    for path in TEMPLATES.rglob("*.j2"):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)="([^"]*radar-[a-z-]+\.(?:js|css))"', text):
            raise AssertionError(f"{path.name} references {match.group(1)} without asset()")


# --- the new bundles use the shared contracts ---------------------------------------

def test_new_bundles_delegate_cards_and_hydration():
    for name, source in (("workbench", WORKBENCH_JS), ("random", RANDOM_JS)):
        assert "RadarCard.buildCard" in source and "RadarUI.hydrate" in source, name
        assert "innerHTML" not in source, name
    assert '"data/run/"' in WORKBENCH_JS and '"data/random/"' in RANDOM_JS


def test_card_shows_scorer_provenance():
    assert 'element("span", "provenance", scoredBy)' in CARD_JS
    assert ".provenance{" in UI_CSS


def test_reading_bundle_uses_the_shared_contracts():
    for contract in ('"radar:mark:"', '"data/day/"', "/manifest.json", "anchor_pages", "RadarCard.buildCard",
                     "RadarUI.hydrate", "navigator.clipboard"):
        assert contract in READING_JS, contract
    assert 'replace(/[^A-Za-z0-9_-]/g, "-")' in READING_JS


def test_the_token_only_ever_travels_to_github():
    assert UI_JS.count('fetch("https://api.github.com"') == 1
    assert UI_JS.count("Bearer") == 1 and "github_pat_" not in UI_JS
    for pattern in ("console.log", "console.warn", "console.error"):
        assert pattern not in UI_JS, pattern
    assert "return Boolean(syncToken() && repoSlug());" in UI_JS


# --- parsing -------------------------------------------------------------------------

def test_the_bundles_are_all_found():
    assert {p.name for p in STATIC.glob("*.js")} == {n for n in assets.BUNDLES if n.endswith(".js")}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed on this machine")
@pytest.mark.parametrize("name", [n for n in assets.BUNDLES if n.endswith(".js")])
def test_the_bundle_parses(name):
    result = subprocess.run(["node", "--check", str(STATIC / name)], capture_output=True, text=True)
    assert result.returncode == 0, f"{name} does not parse:\n{result.stderr}"
