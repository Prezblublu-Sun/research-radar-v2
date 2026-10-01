from __future__ import annotations

import datetime as dt

from radar.pipeline import manifest


def _utc(*args):
    return dt.datetime(*args, tzinfo=dt.timezone.utc)


def test_price_window_peak_and_off_peak():
    assert manifest.price_window(_utc(2026, 9, 28, 2, 0)) == "peak"       # Monday 02:00
    assert manifest.price_window(_utc(2026, 9, 28, 7, 30)) == "peak"      # Monday 07:30
    assert manifest.price_window(_utc(2026, 9, 28, 12, 0)) == "off_peak"  # Monday noon
    assert manifest.price_window(_utc(2026, 10, 3, 2, 0)) == "off_peak"   # Saturday


def test_usage_summary_with_cache_split():
    raws = [{"_usage": {"prompt_tokens": 1000, "cache_hit_tokens": 750, "cache_miss_tokens": 250,
                        "completion_tokens": 300, "reasoning_tokens": 0}}] * 2
    out = manifest.summarize_llm_usage(raws, now=_utc(2026, 10, 3, 2, 0))
    assert out["usage"]["calls"] == 2
    assert out["usage"]["cache_hit_tokens"] == 1500
    assert out["price_window"] == "off_peak"
    expected = (1500 * 0.003 + 500 * 0.15 + 600 * 0.60) / 1e6
    assert abs(out["estimated_usd"] - round(expected, 4)) < 1e-9
    assert out["estimated_usd_per_call"] > 0


def test_usage_summary_without_cache_split_bills_all_prompt_tokens_as_miss():
    raws = [{"_usage": {"prompt_tokens": 1000, "completion_tokens": 0}}, {}, {"_usage": {}}]
    out = manifest.summarize_llm_usage(raws, now=_utc(2026, 10, 3, 2, 0))
    assert out["usage"]["calls"] == 1
    assert out["estimated_usd"] == round(1000 * 0.15 / 1e6, 4)


def test_empty_usage():
    out = manifest.summarize_llm_usage([])
    assert out["usage"]["calls"] == 0 and out["estimated_usd"] == 0.0
