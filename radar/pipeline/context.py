"""Everything a run needs, decided once up front and never mutated.

v1's ``run()`` read environment variables, module globals and the file
system from inside a 436-line function, which is why it could only be
tested by monkeypatching the whole module. A ``RunContext`` is built by the
CLI (or a test) and handed to each stage.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol

from radar.config import Directions
from radar.core.records import new_run_id
from radar.paths import DataRoot
from radar.pipeline.prompt import PromptBundle


class Scorer(Protocol):
    """What the score stage needs; see radar.pipeline.scorer for the three."""

    version: str
    budget_exhausted: str | None

    def score_batch(self, papers: list[dict], config: Directions) -> tuple[list[dict], list[dict]]: ...


@dataclass(frozen=True)
class Fetchers:
    """The three source modules (or stand-ins with the same ``fetch``)."""
    arxiv: Any
    openalex: Any
    pubmed: Any

    @classmethod
    def real(cls) -> "Fetchers":
        from radar.sources import arxiv, openalex, pubmed
        return cls(arxiv=arxiv, openalex=openalex, pubmed=pubmed)


@dataclass(frozen=True)
class RunContext:
    data_root: DataRoot
    config: Directions
    prompt: PromptBundle
    scorer: Scorer
    fetchers: Fetchers
    today: dt.date
    run_id: str = field(default_factory=new_run_id)
    run_type: str = "daily"
    days_back: int = 2
    sources: frozenset[str] = frozenset({"arxiv", "openalex", "pubmed"})
    random_reading: bool = True
    force: bool = False
    git_commit: str = ""
    dry_run: bool = False
    offline: bool = False   # no network beyond the injected fetchers (no Zenodo lookups)

    @property
    def today_iso(self) -> str:
        return self.today.isoformat()
