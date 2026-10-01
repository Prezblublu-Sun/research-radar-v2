"""The browser-safe projection of a paper, and the keys that name it.

Ported from v1 ``build_pages._public_card_record`` and friends. The record
shape is what ``radar-card.js`` renders; keep it stable. v2 adds
``scored_by`` so a card can say which prompt graded it and whether it was
imported from the v1 radar.
"""
from __future__ import annotations

import hashlib
import re

from radar.core import identity
from radar.store.corpus import display_priority

_ANCHOR_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def flatten_text(value) -> str:
    """Join scorer output of any shape into one string (a nested dict once
    killed a v1 build)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(t for t in (flatten_text(v) for v in value.values()) if t)
    if isinstance(value, (list, tuple, set)):
        return " ".join(t for t in (flatten_text(v) for v in value) if t)
    return str(value)


def public_identity_key(paper: dict, bucket_date: str = "", position: int | None = None) -> str:
    """The stable UI key: the public identity, or a deterministic ``noid:``."""
    strict = paper.get("identity_key") or identity.identity_key(paper)
    if strict:
        return strict
    seed = "|".join((bucket_date or str(paper.get("date") or ""),
                     "" if position is None else str(position),
                     str(paper.get("source") or ""), str(paper.get("title") or ""))).encode("utf-8")
    return "noid:" + hashlib.sha1(seed).hexdigest()[:16]


def anchor_id(identity_key: str) -> str:
    """Fragment-safe anchor; ``radar-reading.js`` derives the same one."""
    return _ANCHOR_UNSAFE.sub("-", identity_key)


def public_keys_for_day(papers: list[dict], bucket_date: str) -> dict[int, tuple[str, str]]:
    """``id(paper) -> (identity, anchor)``, with collisions suffixed deterministically."""
    result: dict[int, tuple[str, str]] = {}
    used: set[str] = set()
    for position, paper in enumerate(papers):
        ident = public_identity_key(paper, bucket_date, position)
        base = anchor_id(ident)
        anchor = base
        if anchor in used:
            suffix = hashlib.sha1(ident.encode("utf-8")).hexdigest()[:8]
            anchor = f"{base}--{suffix}"
            counter = 2
            while anchor in used:
                anchor = f"{base}--{suffix}-{counter}"
                counter += 1
        used.add(anchor)
        result[id(paper)] = (ident, anchor)
    return result


def scored_by(paper: dict) -> dict:
    provenance = paper.get("provenance") or {}
    out = {"prompt": str(paper.get("scorer_version") or ""), "origin": str(provenance.get("origin") or "")}
    if provenance.get("v1_direction_name"):
        out["v1_direction_name"] = str(provenance["v1_direction_name"])
    return out


def public_card_record(bucket_date: str, paper: dict, position: int, config,
                       identity_key: str | None = None, anchor: str | None = None,
                       visual: dict | None = None) -> dict:
    llm = paper.get("llm") or {}
    direction = paper.get("direction") or ""
    priority = display_priority(paper)
    ident = identity_key or public_identity_key(paper, bucket_date, position)
    authors = [str(a) for a in (paper.get("authors") or [])]
    corresponding = []
    for item in paper.get("corresponding_authors") or []:
        if isinstance(item, dict) and item.get("affiliation"):
            corresponding.append({"name": str(item.get("name") or ""),
                                  "affiliation": str(item.get("affiliation") or ""),
                                  "inferred": bool(item.get("inferred"))})
    record = {
        "identity_key": ident,
        "anchor": anchor or anchor_id(ident),
        "date": bucket_date,
        "title": paper.get("title") or "",
        "authors": authors[:5],
        "authors_truncated": len(authors) > 5,
        "venue": paper.get("venue") or "",
        "source": paper.get("source") or "",
        "doi": paper.get("doi") or "",
        "url": paper.get("url") or "",
        "direction": direction,
        "direction_name": paper.get("direction_name") or (config.display_name(direction) if direction else ""),
        "direction_color": config.color(direction) if direction else "#667085",
        "priority": priority,
        "priority_label": "待评分" if priority == "Unscored" else priority,
        "relevance_level": llm.get("relevance_level") or "",
        "read_action": llm.get("read_action") or "",
        "validation_kind": llm.get("validation_kind") or "",
        "flags": {key: bool((llm.get("flags") or {}).get(key)) for key in (
            "has_experimental_validation", "has_uncertainty_quantification",
            "is_patient_specific", "is_review")},
        "first_author_affiliation": paper.get("first_author_affiliation") or "",
        "corresponding_authors": corresponding,
        "relevance_to_user": llm.get("relevance_to_user") or "",
        "why_not_core": llm.get("why_not_core") or "",
        "summary_zh": llm.get("summary_zh") or {},
        "summary_en": llm.get("summary_en") or {},
        "key_terms": llm.get("key_terms") or [],
        "tags": llm.get("tags") or [],
        "first_seen_at": paper.get("first_seen_at") or "",
        "scored_by": scored_by(paper),
    }
    if paper.get("crossover"):
        record["crossover"] = paper["crossover"]
    if visual is not None:
        record["visual"] = visual
    return record
