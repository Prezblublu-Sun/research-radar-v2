"""Stage 2: one record per work, and nothing the corpus already holds.

Ported from v1 ``aggregator.aggregate`` minus its file I/O: the set of
already-seen keys is an input (``radar.store.seen`` derives it from the run
logs) and nothing here writes anything. Pure, so it is tested directly.

When two sources return the same work, the record with an abstract wins;
between an arXiv record and a published one with a DOI, the published one
wins (richer metadata) and remembers the preprint in ``also_seen_in``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from radar.core import identity


@dataclass
class DedupResult:
    papers: list[dict] = field(default_factory=list)
    new_keys: set[str] = field(default_factory=set)
    already_seen: int = 0
    merged: int = 0


def dedup_key(paper: dict, aliases: dict | None = None) -> str:
    """Canonical identity, or a source-scoped fallback for records with none."""
    return identity.canonical_key(paper, aliases) or f"{paper.get('source', '?')}:{paper.get('id', '')}"


def dedup_stage(paper_lists: Iterable[list[dict]], seen: set[str],
                aliases: dict | None = None, force: bool = False) -> DedupResult:
    result = DedupResult()
    keep: dict[str, dict] = {}
    for papers in paper_lists:
        for paper in papers:
            key = dedup_key(paper, aliases)
            if key in seen and not force:
                result.already_seen += 1
                continue
            existing = keep.get(key)
            if existing is None:
                keep[key] = paper
                result.new_keys.add(key)
                continue
            result.merged += 1
            if (not existing.get("abstract")) and paper.get("abstract"):
                paper.setdefault("also_seen_in", existing.get("source"))
                keep[key] = paper
            elif (existing.get("source") == "arxiv"
                  and paper.get("source") in ("openalex", "pubmed")
                  and identity.normalize_doi(paper.get("doi"))):
                paper["also_seen_in"] = existing.get("source")
                keep[key] = paper
            else:
                existing.setdefault("also_seen_in", paper.get("source"))
    result.papers = list(keep.values())
    return result
