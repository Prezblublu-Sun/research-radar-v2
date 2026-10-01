"""The marks trust boundary and the cross-device merge (ported from v1)."""
from __future__ import annotations

import json

import pytest

from radar.store import marks as ms


def _payload(**marks):
    return {"schema_version": 1, "device": "dev-1a2b3c4d", "updated_at": "2026-09-22T12:00:00Z", "marks": marks}


def test_a_well_formed_payload_round_trips():
    out = ms.validate_payload(_payload(**{"doi:10.1/a": {"state": "to-read", "at": "2026-09-22T11:00:00Z",
                                                          "note": " n ", "tags": ["b", "a", "a"], "title": "T"}}))
    mark = out["marks"]["doi:10.1/a"]
    assert mark["state"] == "to-read" and mark["note"] == "n" and mark["tags"] == ["a", "b"] and mark["title"] == "T"


@pytest.mark.parametrize("device", ["../x", "Dev 1", "a", "x" * 40, "dev/one", ""])
def test_a_device_name_can_never_escape_the_marks_directory(device):
    with pytest.raises(ms.MarksPayloadError):
        ms.validate_payload({**_payload(), "device": device})


def test_device_case_is_normalised_and_traversal_refused(data_root):
    assert ms.validate_payload({**_payload(), "device": "DEV-1A2B"})["device"] == "dev-1a2b"
    with pytest.raises(ms.MarksPayloadError):
        ms.device_path(data_root, "../../etc")


@pytest.mark.parametrize("key", ["", "doi:", "title:x", "doi:has space", "x" * 300])
def test_identity_keys_are_checked(key):
    with pytest.raises(ms.MarksPayloadError):
        ms.validate_payload(_payload(**{key: {"state": "read"}}))


def test_unknown_states_timestamps_and_schema_are_rejected():
    with pytest.raises(ms.MarksPayloadError):
        ms.validate_payload(_payload(**{"doi:10.1/a": {"state": "starred"}}))
    with pytest.raises(ms.MarksPayloadError):
        ms.validate_payload(_payload(**{"doi:10.1/a": {"state": "read", "at": "yesterday"}}))
    with pytest.raises(ms.MarksPayloadError):
        ms.validate_payload({**_payload(), "schema_version": 2})


def test_legacy_interesting_becomes_read_plus_tag():
    out = ms.validate_payload(_payload(**{"doi:10.1/a": {"state": "interesting"}}))
    assert out["marks"]["doi:10.1/a"] == {"state": "read", "tags": [ms.INSPIRING_TAG], "at": "", "note": "",
                                           "title": "", "date": "", "direction": "", "priority": ""}


def test_empty_marks_are_dropped_and_tombstones_kept():
    out = ms.validate_payload(_payload(**{"doi:10.1/empty": {"state": ""},
                                          "doi:10.1/tomb": {"state": "", "at": "2026-09-22T11:00:00Z"}}))
    assert list(out["marks"]) == ["doi:10.1/tomb"] and ms.is_tombstone(out["marks"]["doi:10.1/tomb"])


def test_extract_accepts_a_fenced_block_or_a_bare_object():
    body = "hello\n```json\n" + json.dumps(_payload()) + "\n```\nbye"
    assert ms.extract_payload(body)["device"] == "dev-1a2b3c4d"
    assert ms.extract_payload(json.dumps(_payload()))["device"] == "dev-1a2b3c4d"
    for bad in ("", "no json here", "```json\n{broken\n```"):
        with pytest.raises(ms.MarksPayloadError):
            ms.extract_payload(bad)


def test_write_then_load_and_newest_mark_wins_across_devices(data_root):
    ms.write_device(data_root, ms.validate_payload({**_payload(**{"doi:10.1/a": {"state": "to-read", "at": "2026-09-22T11:00:00Z"}}),
                                                     "device": "dev-aaaa"}))
    ms.write_device(data_root, ms.validate_payload({**_payload(**{"doi:10.1/a": {"state": "read", "at": "2026-09-23T11:00:00Z"}}),
                                                     "device": "dev-bbbb"}))
    merged = ms.load_all(data_root)
    assert merged["doi:10.1/a"]["state"] == "read" and merged["doi:10.1/a"]["device"] == "dev-bbbb"
    assert (data_root.marks / "dev-aaaa.json").read_text(encoding="utf-8").startswith('{\n "device": "dev-aaaa",')


def test_clearing_on_one_device_is_not_resurrected_by_another(data_root):
    ms.write_device(data_root, ms.validate_payload({**_payload(**{"doi:10.1/a": {"state": "to-read", "at": "2026-09-22T11:00:00Z"}}),
                                                     "device": "dev-aaaa"}))
    ms.write_device(data_root, ms.validate_payload({**_payload(**{"doi:10.1/a": {"state": "", "at": "2026-09-24T11:00:00Z"}}),
                                                     "device": "dev-bbbb"}))
    assert ms.load_all(data_root) == {}


def test_a_corrupt_device_file_does_not_break_the_merge(data_root):
    data_root.marks.mkdir(parents=True)
    (data_root.marks / "dev-bad.json").write_text("{oops", encoding="utf-8")
    ms.write_device(data_root, ms.validate_payload(_payload(**{"doi:10.1/a": {"state": "read", "tags": ["x"]}})))
    merged = ms.load_all(data_root)
    assert list(merged) == ["doi:10.1/a"]
    assert ms.by_state(merged, "read")[0][0] == "doi:10.1/a" and ms.tag_counts(merged) == {"x": 1}
    assert ms.by_tag(merged, "x")[0][0] == "doi:10.1/a"
