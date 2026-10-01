from __future__ import annotations

import datetime as dt

import pytest

from radar.core import records


def test_run_id_is_sortable_and_filename_safe():
    now = dt.datetime(2026, 10, 2, 12, 17, 3, tzinfo=dt.timezone.utc)
    rid = records.new_run_id(now)
    assert rid == "2026-10-02T121703Z"
    assert records.is_run_id(rid)
    assert not records.is_run_id("2026-10-02")
    assert ":" not in rid and "/" not in rid


def test_utc_now_iso_uses_zulu_seconds():
    now = dt.datetime(2026, 10, 2, 12, 17, 3, 999, tzinfo=dt.timezone.utc)
    assert records.utc_now_iso(now) == "2026-10-02T12:17:03Z"


@pytest.mark.parametrize("date, precision", [
    ("2024-03-15", "day"), ("2024-03-01", "month"), ("2024-01-01", "year"),
    ("2024-12-01", "month"), ("", "year"),
])
def test_infer_date_precision(date, precision):
    assert records.infer_date_precision(date) == precision
