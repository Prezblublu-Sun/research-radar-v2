"""Writes that cannot leave half a file behind, and run logs that cannot be
rewritten."""
from __future__ import annotations

import json

import pytest

from radar.core import atomic


def test_atomic_json_round_trip_and_no_tmp_left(tmp_path):
    path = tmp_path / "nested" / "x.json"
    atomic.atomic_write_json(path, {"b": 1, "a": [1, 2]})
    assert json.loads(path.read_text(encoding="utf-8")) == {"b": 1, "a": [1, 2]}
    assert list(path.parent.iterdir()) == [path]


def test_atomic_text_replaces_existing_content(tmp_path):
    path = tmp_path / "t.txt"
    atomic.atomic_write_text(path, "one")
    atomic.atomic_write_text(path, "two")
    assert path.read_text(encoding="utf-8") == "two"


def test_jsonl_new_writes_one_object_per_line_with_lf(tmp_path):
    path = tmp_path / "runs" / "2026" / "r.jsonl"
    n = atomic.write_jsonl_new(path, [{"ck": "doi:a", "x": "中"}, {"ck": "doi:b"}])
    assert n == 2
    raw = path.read_bytes()
    assert b"\r\n" not in raw and raw.endswith(b"\n")
    lines = raw.decode("utf-8").splitlines()
    assert json.loads(lines[0]) == {"ck": "doi:a", "x": "中"}
    assert "中" in lines[0]  # ensure_ascii=False keeps the data readable


def test_jsonl_new_refuses_to_overwrite(tmp_path):
    path = tmp_path / "r.jsonl"
    atomic.write_jsonl_new(path, [{"ck": "doi:a"}])
    with pytest.raises(atomic.WouldOverwrite):
        atomic.write_jsonl_new(path, [{"ck": "doi:b"}])
    assert json.loads(path.read_text(encoding="utf-8")) == {"ck": "doi:a"}
    assert isinstance(atomic.WouldOverwrite("x"), FileExistsError)


def test_jsonl_new_failure_mid_stream_leaves_no_file(tmp_path):
    path = tmp_path / "r.jsonl"

    def lines():
        yield {"ck": "doi:a"}
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        atomic.write_jsonl_new(path, lines())
    assert not path.exists()
