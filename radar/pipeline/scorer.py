"""The three scorers: DeepSeek for real, DryRun for pipelines without secrets,
Replay for tests and re-rendering.

Ported from v1 ``llm_scorer.py`` with its retry semantics intact (three
attempts, the second and third with a strict-JSON nudge; a failure is
``priority: None`` + ``scorer_failed: True``, never a silent "Low") and its
HTTP 402 fuse (one insufficient-balance error stops every remaining call in
the batch). Three things changed:

* no client is built at import — ``DeepSeekScorer`` builds it on first use,
  so importing the pipeline never needs a credential;
* the fuse is instance state, not a module global;
* the failure-dump directory and the prompt are injected, not computed from
  ``__file__``.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from radar.config import Directions
from radar.core.atomic import atomic_write_json
from radar.pipeline import prompt as _prompt
from radar.pipeline.prompt import PromptBundle
from radar.pipeline.router import crossovers

STRICT_JSON_NUDGE = (
    "IMPORTANT: Your previous response was not valid JSON. Output ONLY a "
    "single JSON object. No markdown, no code fences, no explanation."
)
MAX_ATTEMPTS = 3
FAILED = {"priority": None, "scorer_failed": True}


class ScorerBudgetError(RuntimeError):
    """DeepSeek reported insufficient balance (HTTP 402)."""


def _is_budget_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status == 402:
        return True
    text = str(exc).lower()
    return "insufficient balance" in text or "402" in text


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------

@dataclass
class DryRunScorer:
    """Marks every paper as unscored; costs nothing, needs nothing."""
    version: str = "dry-run"
    budget_exhausted: str | None = None

    def score_batch(self, papers, config):
        for paper in papers:
            paper["llm"] = {"priority": None, "dry_run": True}
        return papers, [{} for _ in papers]


@dataclass
class ReplayScorer:
    """Answers from a mapping of identity -> llm dict; unknown papers fail.

    Tests use it to drive the pipeline deterministically; the importer uses
    it to carry v1 verdicts through the v2 stages unchanged.
    """
    answers: dict[str, dict]
    key: str = "doi"
    default: dict | None = None     # answer for papers not in the mapping
    version: str = "replay"
    budget_exhausted: str | None = None

    def score_batch(self, papers, config):
        raws = []
        for paper in papers:
            answer = self.answers.get(str(paper.get(self.key) or ""), self.default)
            paper["llm"] = dict(answer) if answer is not None else {
                **FAILED, "scorer_failed_reason": "no replay answer",
                "scorer_failed_attempts": 0}
            raws.append({"_usage": {}})
        return papers, raws


# ---------------------------------------------------------------------------
# The real one
# ---------------------------------------------------------------------------

@dataclass
class DeepSeekScorer:
    prompt: PromptBundle
    failures_dir: pathlib.Path | None = None
    model: str = field(default_factory=lambda: os.environ.get("MODEL_NAME", "deepseek-v4-flash"))
    temperature: float = field(default_factory=lambda: float(os.environ.get("LLM_TEMPERATURE", "0.2")))
    max_tokens: int = field(default_factory=lambda: int(os.environ.get("LLM_MAX_TOKENS", "2000")))
    thinking: str = field(default_factory=lambda: (os.environ.get("LLM_THINKING", "disabled").strip().lower() or "disabled"))
    concurrency: int = field(default_factory=lambda: int(os.environ.get("LLM_CONCURRENCY", "8")))
    timeout: float = field(default_factory=lambda: float(os.environ.get("LLM_TIMEOUT", "60")))
    max_retries: int = field(default_factory=lambda: int(os.environ.get("LLM_MAX_RETRIES", "3")))
    budget_exhausted: str | None = None
    _client: object = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def version(self) -> str:
        return self.prompt.version

    # --- client ---------------------------------------------------------------
    def client(self):
        if self._client is None:
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is required for scoring; "
                                   "use --dry-run for a run without it")
            from openai import OpenAI
            self._client = OpenAI(
                api_key=api_key,
                base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    # --- one call -------------------------------------------------------------
    def score(self, paper: dict, config: Directions, system_prompt: str,
              nudge: bool = False) -> dict:
        messages = [
            {"role": "system", "content": (STRICT_JSON_NUDGE + "\n\n" + system_prompt) if nudge else system_prompt},
            {"role": "user", "content": _prompt.user_message(
                paper, config, crossovers(paper, config.crossover_pairs))},
        ]
        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self.thinking == "disabled":
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = self.client().chat.completions.create(**kwargs)
        raw = response.choices[0].message.content
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            error.raw_content = raw  # type: ignore[attr-defined]
            raise
        parsed["_raw_model"] = getattr(response, "model", "") or ""
        parsed["_usage"] = _usage_dict(getattr(response, "usage", None))
        return parsed

    # --- one paper, three attempts -------------------------------------------
    def _score_one(self, paper: dict, config: Directions, system_prompt: str) -> tuple[dict, dict]:
        if self.budget_exhausted:
            paper["llm"] = {**FAILED, "scorer_failed_reason": f"skipped: {self.budget_exhausted}",
                            "scorer_failed_attempts": 0}
            return paper, {}
        result = None
        last: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = self.score(paper, config, system_prompt, nudge=attempt > 1)
                break
            except Exception as error:  # noqa: BLE001 — every failure is recorded
                last = error
                self._dump_failure(paper, getattr(error, "raw_content", None), error)
                if _is_budget_error(error):
                    with self._lock:
                        self.budget_exhausted = str(error)[:200]
                    break
        if result is not None:
            raw = {"_raw_model": result.pop("_raw_model", ""), "_usage": result.pop("_usage", {})}
            paper["llm"] = result
            return paper, raw
        paper["llm"] = {**FAILED, "scorer_failed_reason": str(last),
                        "scorer_failed_attempts": MAX_ATTEMPTS}
        return paper, {}

    def score_batch(self, papers: list[dict], config: Directions) -> tuple[list[dict], list[dict]]:
        """Score every paper, concurrently, preserving input order."""
        if not papers:
            return [], []
        system_prompt = _prompt.system_prompt(self.prompt, config)
        workers = max(1, min(self.concurrency, len(papers)))
        if workers == 1:
            pairs = [self._score_one(p, config, system_prompt) for p in papers]
        else:
            slots: list[tuple[dict, dict] | None] = [None] * len(papers)
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._score_one, p, config, system_prompt): i
                           for i, p in enumerate(papers)}
                for future in concurrent.futures.as_completed(futures):
                    slots[futures[future]] = future.result()
            pairs = [slot for slot in slots if slot is not None]
        return [p for p, _ in pairs], [raw for _, raw in pairs]

    # --- failure dumps ---------------------------------------------------------
    def _dump_failure(self, paper: dict, raw: str | None, error: BaseException) -> None:
        if self.failures_dir is None:
            return
        try:
            now = datetime.now(timezone.utc)
            ident = paper.get("doi") or paper.get("arxiv_id") or paper.get("id") or "noid"
            slug = str(ident).replace("/", "_").replace("\\", "_").strip()[:120] or "noid"
            path = self.failures_dir / f"{now:%Y-%m-%d}_{slug}.json"
            counter = 1
            while path.exists():
                path = self.failures_dir / f"{now:%Y-%m-%d}_{slug}_{counter}.json"
                counter += 1
            atomic_write_json(path, {
                "timestamp_utc": now.isoformat(),
                "doi": paper.get("doi"), "arxiv_id": paper.get("arxiv_id"),
                "title": (paper.get("title") or "")[:300],
                "direction": paper.get("direction"),
                "error_class": type(error).__name__, "error_message": str(error),
                "raw_response": raw, "raw_response_captured": raw is not None,
                "model": self.model, "prompt_file": self.prompt.file,
            })
        except Exception:  # noqa: BLE001 — a dump must never break scoring
            pass


def _usage_dict(usage) -> dict:
    """Flatten the SDK usage object; DeepSeek adds prompt-cache and reasoning counts."""
    if usage is None:
        return {}
    get = lambda name: getattr(usage, name, None)  # noqa: E731
    details = get("completion_tokens_details")
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    out = {
        "prompt_tokens": int(get("prompt_tokens") or 0),
        "completion_tokens": int(get("completion_tokens") or 0),
        "total_tokens": int(get("total_tokens") or 0),
        "cache_hit_tokens": int(get("prompt_cache_hit_tokens") or 0),
        "cache_miss_tokens": int(get("prompt_cache_miss_tokens") or 0),
        "reasoning_tokens": int(reasoning or 0),
    }
    return out
