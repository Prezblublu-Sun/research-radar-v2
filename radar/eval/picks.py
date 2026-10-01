"""The ground-truth set: papers the reader actually marked 待读.

The input is the reading list's "复制当前清单为 Markdown" output — a flat
list, one line per paper::

    - [Title](https://doi.org/10.…) — 2026-09-24 · 待阅读
    - Title without a link — 2026-05-05 · 待阅读
    - [arxiv:2604.03788v3](https://arxiv.org/abs/2604.03788v3) — 待阅读

Identity comes from the link; a line with no link can only be matched by
title. ``expected_direction`` is not in the list (there are no headings) —
it is the v2 router's primary direction on the resolved record, recorded so
later config edits can be diffed against it.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass

from radar.core import identity
from radar.pipeline import router

_LINKED = re.compile(r"^\[(?P<title>[^\]]+)\]\((?P<url>[^)]+)\)(?P<rest>.*)$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _split_line(line: str) -> tuple[str, str, str] | None:
    """``(title, url, rest)`` for one ``- …`` bullet, or None."""
    body = line.lstrip()[2:].strip()
    match = _LINKED.match(body)
    if match:
        return match.group("title").strip(), match.group("url").strip(), match.group("rest")
    title, _, rest = body.partition("—")
    return title.strip(), "", rest


@dataclass
class Pick:
    title: str
    identity_key: str          # doi:… / arxiv:… / "" when only a title is known
    ck: str                    # canonical key for joining to corpus records
    date: str


def parse_picks_md(text: str) -> list[Pick]:
    picks: list[Pick] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.lstrip().startswith("- "):
            continue
        parts = _split_line(line)
        if not parts or not parts[0]:
            continue
        title, url, rest = parts
        found = _DATE.search(rest)
        date = found.group(0) if found else ""
        idk, ck = "", ""
        if "doi.org/" in url:
            doi = url.split("doi.org/", 1)[1]
            idk, ck = f"doi:{doi}", identity.canonical_key({"doi": doi})
        elif "arxiv.org/abs/" in url:
            raw = url.split("arxiv.org/abs/", 1)[1]
            idk, ck = f"arxiv:{raw}", identity.canonical_key({"arxiv_id": raw})
        if title.startswith(("doi:", "arxiv:")) and not url:
            idk = title
            ck = identity.canonical_key({"doi": title[4:]} if title.startswith("doi:") else {"arxiv_id": title[6:]})
        dedup = ck or title.lower()
        if dedup in seen:
            continue  # the list repeats a paper when it carries two identities
        seen.add(dedup)
        picks.append(Pick(title=title, identity_key=idk, ck=ck, date=date))
    return picks


def resolve_picks(picks: list[Pick], records: list[dict], config) -> tuple[list[dict], list[Pick]]:
    """Join picks to corpus records (by ck, then exact title) and route them."""
    by_ck = {identity.canonical_key(r): r for r in records if identity.canonical_key(r)}
    by_title = {(r.get("title") or "").strip().lower(): r for r in records}
    resolved, missing = [], []
    for pick in picks:
        record = by_ck.get(pick.ck) if pick.ck else None
        if record is None:
            record = by_title.get(pick.title.strip().lower())
        if record is None:
            missing.append(pick)
            continue
        probe = {"title": record.get("title", ""), "abstract": record.get("abstract", "")}
        router.route(probe, config.directions, config.exclusions)
        resolved.append({
            "ck": pick.ck or identity.canonical_key(record),
            "identity_key": pick.identity_key or identity.identity_key(record),
            "title": record.get("title", ""),
            "abstract": (record.get("abstract") or "")[:3000],
            "venue": record.get("venue", ""),
            "date": pick.date or record.get("date", ""),
            "source": record.get("source", ""),
            "v1_direction": record.get("direction"),
            "v1_priority": (record.get("llm") or {}).get("priority"),
            "v1_relevance": (record.get("llm") or {}).get("relevance_level"),
            "expected_direction": probe.get("direction"),
            "expected_directions": probe.get("directions") or [],
        })
    return resolved, missing


def write_fixture(path: pathlib.Path, resolved: list[dict]) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for record in resolved:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_fixture(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def route_report(fixture: list[dict], config) -> dict:
    """Recall of the router over the fixture, plus a confusion table."""
    routed = 0
    agree = 0
    table: dict[str, dict[str, int]] = {}
    misses: list[str] = []
    for record in fixture:
        probe = {"title": record["title"], "abstract": record.get("abstract", "")}
        router.route(probe, config.directions, config.exclusions)
        primary = probe.get("direction") or "UNROUTED"
        expected = record.get("expected_direction") or "UNROUTED"
        table.setdefault(expected, {}).setdefault(primary, 0)
        table[expected][primary] += 1
        if primary != "UNROUTED":
            routed += 1
        else:
            misses.append(record["title"])
        if primary == expected:
            agree += 1
    n = len(fixture) or 1
    return {"n": len(fixture), "routed": routed, "recall": routed / n,
            "agreement": agree / n, "table": table, "misses": misses}
