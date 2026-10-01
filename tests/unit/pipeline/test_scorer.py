"""Scorer contracts: three attempts with a strict-JSON nudge, honest failure
records, and the 402 fuse (ported from v1 test_scorer_budget)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from radar.pipeline import scorer as sc


class _InsufficientBalance(Exception):
    status_code = 402

    def __str__(self):
        return "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"


_GOOD = json.dumps({"priority": "High", "relevance_to_user": "yes", "tags": ["t"]})


def _fake_client(responses, calls):
    it = iter(responses)

    def create(**kwargs):
        calls.append(kwargs)
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                                prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20,
                                completion_tokens_details=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=item))],
                               model="deepseek-v4-flash-2026", usage=usage)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _scorer(prompt_bundle, responses, calls):
    return sc.DeepSeekScorer(prompt=prompt_bundle, concurrency=1,
                             _client=_fake_client(responses, calls))


def _paper(doi, cfg):
    return {"doi": doi, "title": "t", "abstract": "a", "direction": cfg.keys[0], "directions": [cfg.keys[0]]}


def test_success_records_llm_and_usage(prompt_bundle, cfg):
    calls: list = []
    s = _scorer(prompt_bundle, [_GOOD], calls)
    out, raws = s.score_batch([_paper("10.1/a", cfg)], cfg)
    assert out[0]["llm"]["priority"] == "High"
    assert "_usage" not in out[0]["llm"]
    assert raws[0]["_usage"]["cache_hit_tokens"] == 80
    assert raws[0]["_raw_model"] == "deepseek-v4-flash-2026"
    assert calls[0]["messages"][0]["role"] == "system"
    assert sc.STRICT_JSON_NUDGE not in calls[0]["messages"][0]["content"]
    assert "{direction_context}" not in calls[0]["messages"][0]["content"]
    assert calls[0]["response_format"] == {"type": "json_object"}


def test_bad_json_is_retried_with_nudge_then_succeeds(prompt_bundle, cfg):
    calls: list = []
    s = _scorer(prompt_bundle, ["not json", _GOOD], calls)
    out, _ = s.score_batch([_paper("10.1/a", cfg)], cfg)
    assert len(calls) == 2
    assert calls[1]["messages"][0]["content"].startswith(sc.STRICT_JSON_NUDGE)
    assert out[0]["llm"]["priority"] == "High"


def test_three_failures_yield_honest_failure_record(prompt_bundle, cfg, tmp_path):
    calls: list = []
    s = _scorer(prompt_bundle, [ValueError("x"), ValueError("y"), ValueError("z")], calls)
    s.failures_dir = tmp_path
    out, raws = s.score_batch([_paper("10.1/a", cfg)], cfg)
    assert len(calls) == sc.MAX_ATTEMPTS
    assert out[0]["llm"]["priority"] is None
    assert out[0]["llm"]["scorer_failed"] is True
    assert out[0]["llm"]["scorer_failed_attempts"] == 3
    assert raws == [{}]
    assert len(list(tmp_path.glob("*.json"))) == 3  # one dump per attempt


def test_first_402_stops_all_further_calls(prompt_bundle, cfg):
    calls: list = []
    s = _scorer(prompt_bundle, [_GOOD, _InsufficientBalance(), _GOOD, _GOOD], calls)
    papers = [_paper(f"10.1/{i}", cfg) for i in "abcd"]
    out, _ = s.score_batch(papers, cfg)
    assert len(calls) == 2
    assert out[0]["llm"]["priority"] == "High"
    assert out[1]["llm"]["scorer_failed"] is True and "402" in out[1]["llm"]["scorer_failed_reason"]
    for p in out[2:]:
        assert p["llm"]["scorer_failed_reason"].startswith("skipped:")
        assert p["llm"]["scorer_failed_attempts"] == 0
    assert "Insufficient Balance" in s.budget_exhausted


def test_fuse_is_instance_state(prompt_bundle, cfg):
    tripped = _scorer(prompt_bundle, [_InsufficientBalance()], [])
    tripped.score_batch([_paper("10.1/a", cfg)], cfg)
    fresh = _scorer(prompt_bundle, [_GOOD], [])
    assert tripped.budget_exhausted and fresh.budget_exhausted is None
    out, _ = fresh.score_batch([_paper("10.1/b", cfg)], cfg)
    assert out[0]["llm"]["priority"] == "High"


def test_no_client_is_built_without_a_key(prompt_bundle, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    s = sc.DeepSeekScorer(prompt=prompt_bundle)
    try:
        s.client()
    except RuntimeError as error:
        assert "dry-run" in str(error)
    else:
        raise AssertionError("expected RuntimeError without a key")


def test_dry_run_and_replay_scorers(cfg):
    papers = [{"doi": "10.1/a"}, {"doi": "10.1/b"}]
    out, raws = sc.DryRunScorer().score_batch(papers, cfg)
    assert all(p["llm"] == {"priority": None, "dry_run": True} for p in out) and raws == [{}, {}]
    replay = sc.ReplayScorer({"10.1/a": {"priority": "Medium"}})
    out, _ = replay.score_batch([{"doi": "10.1/a"}, {"doi": "10.1/b"}], cfg)
    assert out[0]["llm"] == {"priority": "Medium"}
    assert out[1]["llm"]["scorer_failed"] is True
