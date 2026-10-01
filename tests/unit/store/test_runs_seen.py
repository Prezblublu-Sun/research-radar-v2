"""Run-log reading and the derived seen-key set."""
from __future__ import annotations

import json

import pytest

from radar.core.atomic import write_jsonl_new
from radar.store import runs
from radar.store.seen import SeenKeys


def _run(root, run_id, keys, run_type="daily"):
    path = root.run_file(run_id, run_type)
    write_jsonl_new(path, [{"kind": "run", "run_id": run_id, "run_type": run_type, "finished_at": "2026-10-01T12:00:00Z"}]
                    + [{"ck": k, "kind": "paper", "title": "t"} for k in keys])
    return path


def test_list_runs_is_chronological_across_years(data_root):
    _run(data_root, "2026-10-01T120000Z", ["doi:a"])
    _run(data_root, "2025-12-31T235959Z", ["doi:b"])
    _run(data_root, "2026-10-01T090000Z", ["doi:c"], run_type="rescore")
    names = [p.name for p in runs.list_runs(data_root)]
    assert names == ["2025-12-31T235959Z-daily.jsonl", "2026-10-01T090000Z-rescore.jsonl",
                     "2026-10-01T120000Z-daily.jsonl"]


def test_header_and_records_and_scan_keys(data_root):
    path = _run(data_root, "2026-10-01T120000Z", ["doi:10.1/x", "arxiv:2601.1", ""])
    assert runs.read_header(path)["kind"] == "run"
    assert [r["ck"] for r in runs.iter_records(path)] == ["doi:10.1/x", "arxiv:2601.1", ""]
    assert runs.scan_keys(path) == {"doi:10.1/x", "arxiv:2601.1"}   # empty ck is not a key


def test_read_header_rejects_non_run_file(tmp_path):
    bad = tmp_path / "x.jsonl"
    bad.write_text('{"ck":"doi:a"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        runs.read_header(bad)


def test_seen_rebuild_cache_and_invalidation(data_root):
    _run(data_root, "2026-10-01T120000Z", ["doi:a", "doi:b"])
    seen = SeenKeys.load(data_root)
    assert seen.rebuilt and seen.keys == {"doi:a", "doi:b"} and seen.n_files == 1
    seen.save_cache(data_root)
    again = SeenKeys.load(data_root)
    assert not again.rebuilt and again.keys == {"doi:a", "doi:b"}

    _run(data_root, "2026-10-02T120000Z", ["doi:c"])
    fresh = SeenKeys.load(data_root)
    assert fresh.rebuilt and fresh.keys == {"doi:a", "doi:b", "doi:c"}


def test_truncated_cache_only_costs_a_rebuild(data_root):
    _run(data_root, "2026-10-01T120000Z", ["doi:a"])
    SeenKeys.load(data_root).save_cache(data_root)
    cache = data_root.cache / "seen_keys.json"
    cache.write_text(cache.read_text(encoding="utf-8")[:10], encoding="utf-8")
    seen = SeenKeys.load(data_root)
    assert seen.rebuilt and seen.keys == {"doi:a"}


def test_no_runs_means_empty_set(data_root):
    seen = SeenKeys.load(data_root)
    assert len(seen) == 0 and "doi:a" not in seen
