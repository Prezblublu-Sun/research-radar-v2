"""Shared fixtures. Placeholder credentials only: a developer's real shell
values are never overridden (``setdefault``), and nothing here can reach a
real service.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib

import pytest

os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.invalid")
os.environ.setdefault("MODEL_NAME", "deepseek-v4-flash")
os.environ.setdefault("OPENALEX_API_KEY", "test-openalex-key-not-real")

REPO = pathlib.Path(__file__).resolve().parent.parent
FIXTURES = REPO / "tests" / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    return REPO


@pytest.fixture(scope="session")
def cfg():
    from radar import config
    return config.load(REPO / "config" / "directions.yaml")


@pytest.fixture(scope="session")
def prompt_bundle():
    from radar.pipeline import prompt
    return prompt.load(REPO / "prompts", "scorer_v4.txt")


@pytest.fixture(scope="session")
def sample_records() -> list[dict]:
    path = FIXTURES / "fetch" / "sample.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def data_root(tmp_path):
    from radar.paths import DataRoot
    return DataRoot.at(tmp_path / "data")


@pytest.fixture
def paper_factory():
    """``paper("openalex", doi="10.1/x", title=..., abstract=...)`` → fetcher-shaped record."""
    def make(source: str = "arxiv", *, doi: str = "", arxiv_id: str = "", pmid: str = "",
             oa_id: str = "", title: str = "Neural operator surrogate for finite element stress",
             abstract: str = "We train a neural operator surrogate on finite element simulations.",
             date: str = "2026-09-01") -> dict:
        ident = oa_id or (f"pubmed:{pmid}" if pmid else (arxiv_id or doi or title))
        paper = {
            "source": source, "id": ident, "doi": doi, "title": title,
            "abstract": abstract, "authors": ["A. Author"],
            "first_author_affiliation": "", "corresponding_authors": [],
            "venue": "V", "year": int(date[:4]), "date": date,
            "url": f"https://doi.org/{doi}" if doi else ident,
            "cited_by_count": 0, "concepts": [{"id": "C1"}], "categories": [],
        }
        if arxiv_id:
            paper["arxiv_id"] = arxiv_id
        if pmid:
            paper["pmid"] = pmid
        return paper
    return make


@pytest.fixture
def stub_fetchers():
    """``stub_fetchers(arxiv=[...], openalex=[...], pubmed=[...])`` → Fetchers.

    Each stub records the keyword arguments it was called with in ``calls``
    and may raise when given an exception instead of a list.
    """
    from radar.pipeline.context import Fetchers

    def make(arxiv=None, openalex=None, pubmed=None, stats: dict | None = None):
        calls: dict[str, dict] = {}

        class Stub:
            def __init__(self, name, payload):
                self.name, self.payload = name, payload

            def fetch(self, *args, **kwargs):
                calls[self.name] = dict(kwargs)
                if isinstance(self.payload, BaseException):
                    raise self.payload
                if self.name == "openalex" and stats is not None and kwargs.get("stats") is not None:
                    kwargs["stats"].update(stats)
                return [dict(p) for p in (self.payload or [])]

        fetchers = Fetchers(arxiv=Stub("arxiv", arxiv), openalex=Stub("openalex", openalex),
                            pubmed=Stub("pubmed", pubmed))
        object.__setattr__(fetchers, "calls", calls)  # Fetchers is frozen; tests only
        return fetchers
    return make


@pytest.fixture
def make_ctx(cfg, prompt_bundle, data_root):
    """Build a RunContext with sensible test defaults; override by keyword."""
    from radar.pipeline.context import RunContext
    from radar.pipeline.scorer import DryRunScorer

    def make(fetchers, **overrides):
        kwargs = dict(
            data_root=data_root, config=cfg, prompt=prompt_bundle, scorer=DryRunScorer(),
            fetchers=fetchers, today=dt.date(2026, 10, 1), run_id="2026-10-01T120000Z",
            days_back=2, random_reading=False, git_commit="test", offline=True,
        )
        kwargs.update(overrides)
        return RunContext(**kwargs)
    return make
