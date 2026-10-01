"""The run header: the first line of every run log.

What v1 wrote to ``data/manifests/<date>.json`` — git commit, config and
prompt hashes, model, token usage and a list-price estimate, counts, source
health, quality flags — now travels with the data it describes.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from importlib import metadata

from radar.core.records import SCHEMA_VERSION, utc_now_iso

# deepseek-flash list prices, USD per 1M tokens (api-docs.deepseek.com,
# 2026-09). Peak = 01:00-04:00 and 06:00-10:00 UTC Mon-Fri; off-peak is half
# price. Indicative: assumes the whole run was billed in the window it
# started in.
FLASH_USD_PER_M = {
    "peak": {"cache_hit": 0.006, "cache_miss": 0.30, "output": 1.20},
    "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.60},
}
USAGE_KEYS = ("calls", "prompt_tokens", "cache_hit_tokens", "cache_miss_tokens",
              "completion_tokens", "reasoning_tokens")


def git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       stderr=subprocess.DEVNULL).decode().strip()
    except Exception:  # noqa: BLE001
        return os.environ.get("GITHUB_SHA", "")[:7] or "unknown"


def pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except Exception:  # noqa: BLE001
        return "unknown"


def price_window(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.weekday() < 5 and (1 <= now.hour < 4 or 6 <= now.hour < 10):
        return "peak"
    return "off_peak"


def summarize_llm_usage(raws, now: dt.datetime | None = None) -> dict:
    """Aggregate per-call ``_usage`` dicts and estimate the bill."""
    total = {key: 0 for key in USAGE_KEYS}
    for raw in raws or []:
        usage = raw.get("_usage") if isinstance(raw, dict) else None
        if not usage:
            continue
        total["calls"] += 1
        for key in USAGE_KEYS[1:]:
            total[key] += int(usage.get(key) or 0)
    window = price_window(now)
    prices = FLASH_USD_PER_M[window]
    hit, miss = total["cache_hit_tokens"], total["cache_miss_tokens"]
    if not hit and not miss:
        miss = total["prompt_tokens"]  # provider gave no cache split
    usd = (hit * prices["cache_hit"] + miss * prices["cache_miss"]
           + total["completion_tokens"] * prices["output"]) / 1e6
    return {
        "usage": total,
        "price_window": window,
        "estimated_usd": round(usd, 4),
        "estimated_usd_per_call": round(usd / total["calls"], 5) if total["calls"] else 0.0,
    }


def build_header(ctx, *, started_at: str, run_status: str, quality_flags: list[str],
                 window: dict, sources_used: dict, source_status: dict,
                 counts: dict, raws: list[dict], random_reading: dict,
                 model_snapshot: str = "") -> dict:
    scorer = ctx.scorer
    return {
        "kind": "run",
        "schema_version": SCHEMA_VERSION,
        "run_id": ctx.run_id,
        "run_type": ctx.run_type,
        "started_at": started_at,
        "finished_at": utc_now_iso(),
        "git_commit": ctx.git_commit or git_commit(),
        "run_status": run_status,
        "quality_flags": list(quality_flags),
        "config": {
            "directions_yaml": ctx.config.sha,
            "scorer_prompt_file": ctx.prompt.file,
            "scorer_prompt": ctx.prompt.sha,
            "scorer_version": scorer.version,
        },
        "window": window,
        "sources_used": sources_used,
        "source_status": source_status,
        "llm": {
            "model_alias": getattr(scorer, "model", scorer.version),
            "model_snapshot_observed": model_snapshot,
            "base_url": os.environ.get("OPENAI_BASE_URL", ""),
            "temperature": getattr(scorer, "temperature", None),
            "thinking": getattr(scorer, "thinking", None),
            **summarize_llm_usage(raws),
        },
        "counts": counts,
        "random_reading": random_reading,
        "packages": {name: pkg_version(name) for name in ("arxiv", "openai", "requests", "PyYAML", "Jinja2")},
    }
