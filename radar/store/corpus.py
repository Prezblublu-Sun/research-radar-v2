"""The corpus: one merged record per work, read from the run logs.

Nothing here is authoritative; it is a view. The merge rule (ADR-0001 §2.5)
groups every paper line under ``data/runs/`` by its canonical key ``ck``:

* ``identity_key`` and ``first_seen_at`` come from the **earliest** record,
  so anchors and browser marks never move;
* routing, ``llm``, ``date`` and every other field come from the **newest**
  record whose scorer did not fail, so a rescore supersedes a failure
  without rewriting anything;
* ``runs`` lists every run that saw the work and ``first_run_id`` the one
  that introduced it (what the workbench groups by).

Records with no canonical key (no DOI, arXiv id, PMID or OpenAlex id) are
kept as they are and never merged: fuzzy matching is forbidden.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from radar.paths import DataRoot
from radar.store import runs as _runs

PRIORITIES = ("High", "Medium", "Low", "Exclude")
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Unscored": 2, "Low": 3, "Exclude": 4}


def display_priority(paper: dict) -> str:
    """``Unscored`` for an explicit scorer failure, else one of the four."""
    llm = paper.get("llm") or {}
    if llm.get("scorer_failed") is True:
        return "Unscored"
    priority = llm.get("priority")
    return priority if priority in PRIORITIES else "Low"


def priority_counts(papers: list[dict]) -> dict[str, int]:
    counts = {priority: 0 for priority in PRIORITIES}
    for paper in papers:
        key = display_priority(paper)
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass(frozen=True)
class CorpusStats:
    raw_total: int
    unique_total: int
    duplicates_suppressed: int
    priority_counts: dict[str, int]

    def as_dict(self) -> dict:
        return {"raw_total": self.raw_total, "unique_total": self.unique_total,
                "duplicates_suppressed": self.duplicates_suppressed,
                "priority_counts": dict(self.priority_counts)}


@dataclass
class Corpus:
    papers: list[dict] = field(default_factory=list)
    buckets: dict[str, list[dict]] = field(default_factory=dict)   # publication date -> papers
    headers: list[dict] = field(default_factory=list)              # run headers, newest first
    stats: CorpusStats = field(default_factory=lambda: CorpusStats(0, 0, 0, priority_counts([])))

    @property
    def by_first_run(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for paper in self.papers:
            grouped.setdefault(paper.get("first_run_id") or "", []).append(paper)
        return grouped

    @property
    def generated_at(self) -> str:
        return max((str(p.get("scored_at") or "") for p in self.papers), default="")


def _scored_ok(record: dict) -> bool:
    return (record.get("llm") or {}).get("scorer_failed") is not True


def merge_records(records: list[dict]) -> dict:
    """One record from every observation of the same work (see module doc)."""
    ordered = sorted(records, key=lambda r: (str(r.get("first_seen_at") or ""), str(r.get("run_id") or "")))
    earliest = ordered[0]
    valid = [r for r in records if _scored_ok(r)] or list(records)
    newest = max(valid, key=lambda r: (str(r.get("scored_at") or ""), str(r.get("run_id") or "")))
    merged = dict(newest)
    merged["identity_key"] = earliest.get("identity_key") or newest.get("identity_key") or ""
    merged["first_seen_at"] = earliest.get("first_seen_at") or newest.get("first_seen_at") or ""
    merged["first_run_id"] = earliest.get("run_id") or ""
    merged["runs"] = sorted({str(r.get("run_id") or "") for r in records if r.get("run_id")})
    return merged


def bucket_date(paper: dict) -> str:
    date = str(paper.get("date") or "").strip()
    if len(date) >= 10:
        return date[:10]
    return str(paper.get("first_seen_at") or "")[:10] or "undated"


def load_corpus(root: DataRoot) -> Corpus:
    groups: dict[str, list[dict]] = {}
    loose: list[dict] = []
    headers: list[dict] = []
    raw_total = 0
    for path in _runs.list_runs(root):
        try:
            headers.append(_runs.read_header(path))
        except Exception:  # noqa: BLE001 — one bad file must not empty the site
            continue
        for record in _runs.iter_records(path):
            raw_total += 1
            key = record.get("ck") or ""
            if key:
                groups.setdefault(key, []).append(record)
            else:
                loose.append(dict(record, first_run_id=record.get("run_id") or "",
                                  runs=[record.get("run_id") or ""]))
    papers = [merge_records(group) for group in groups.values()] + loose
    papers.sort(key=lambda p: (bucket_date(p), PRIORITY_ORDER.get(display_priority(p), 9), str(p.get("title") or "")))
    buckets: dict[str, list[dict]] = {}
    for paper in papers:
        buckets.setdefault(bucket_date(paper), []).append(paper)
    headers.sort(key=_runs.run_order, reverse=True)
    stats = CorpusStats(raw_total=raw_total, unique_total=len(papers),
                        duplicates_suppressed=raw_total - len(papers),
                        priority_counts=priority_counts(papers))
    return Corpus(papers=papers, buckets=buckets, headers=headers, stats=stats)


def load_random_runs(root: DataRoot, limit: int = 30) -> list[tuple[dict, list[dict]]]:
    """Newest-first ``(header, records)`` from the random-reading stream."""
    if not root.random_reading.is_dir():
        return []
    out: list[tuple[dict, list[dict]]] = []
    for path in sorted(root.random_reading.rglob("*.jsonl"), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                import json
                lines = [json.loads(line) for line in handle if line.strip()]
        except (OSError, ValueError):
            continue
        if not lines or lines[0].get("kind") != "random_reading":
            continue
        out.append((lines[0], [r for r in lines[1:] if r.get("kind") == "paper"]))
        if len(out) >= limit:
            break
    return out
