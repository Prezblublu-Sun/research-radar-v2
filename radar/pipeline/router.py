"""Route a paper to research directions by keyword rules.

Scoring per direction: each matched ``strong_keyword`` scores 2.0, each
``must_pair_with`` pair (both terms present) scores 3.0; a direction with
score >= ``min_score`` (2.0) is matched, so one strong hit routes. Pairs
widen reach, they are not gates — recall over precision.

Two changes from v1's ``direction_router.py``:

* **Plurals match.** v1 tested ``\\bterm\\b``, so ``PINN`` did not match
  ``PINNs`` and ``neural operator`` did not match ``neural operators`` —
  four of the user's 121 hand-picked papers were unroutable for that reason
  alone. A term now also matches with a trailing ``s``.
* **Crossover boost is configuration.** v1 hard-coded one pair. The pairs
  come from ``crossover_boost:`` in directions.yaml, with a reason string
  that is written into the record.
"""
from __future__ import annotations

import re

STRONG_SCORE = 2.0
PAIR_SCORE = 3.0
MIN_SCORE = 2.0
BUMP = {"Low": "Medium", "Medium": "High"}

# Words that mean "this is in our world"; a hard-exclusion term only excludes
# a paper that has none of them.
ANCHORS = ("femoral", "hip implant", "stem", "implant", "biomechanics",
           "biomedical", "bone", "osseointegration", "scaffold", "finite element",
           "surrogate", "neural operator", "physics-informed")

_pattern_cache: dict[str, re.Pattern] = {}


def _pattern(term: str) -> re.Pattern:
    key = term.lower()
    pattern = _pattern_cache.get(key)
    if pattern is None:
        pattern = re.compile(rf"\b{re.escape(key)}s?\b")
        _pattern_cache[key] = pattern
    return pattern


def contains(text: str, term: str) -> bool:
    return _pattern(term).search(text) is not None


def _text(paper: dict) -> str:
    return f"{paper.get('title', '')} {paper.get('abstract', '')}".lower()


def is_excluded(text: str, exclusions: dict) -> bool:
    for term in exclusions.get("hard_exclude_if_only_about") or []:
        if contains(text, term) and not any(contains(text, a) for a in ANCHORS):
            return True
    return False


def score_direction(text: str, cfg: dict) -> tuple[float, list[str]]:
    score = 0.0
    matched: list[str] = []
    for kw in cfg.get("strong_keywords") or []:
        if contains(text, kw):
            score += STRONG_SCORE
            matched.append(kw)
    for pair in cfg.get("must_pair_with") or []:
        if all(contains(text, term) for term in pair):
            score += PAIR_SCORE
            matched.append(" + ".join(pair))
    return score, matched


def route(paper: dict, directions: dict, exclusions: dict | None = None,
          min_score: float = MIN_SCORE) -> dict:
    """Set ``directions``, ``direction``, ``direction_name``, ``routing_matches``
    on the paper (in place) and return it. ``directions`` is the raw mapping
    from ``radar.config.Directions.directions``.
    """
    text = _text(paper)
    if is_excluded(text, exclusions or {}):
        paper["directions"] = []
        paper["direction"] = None
        paper["direction_name"] = None
        paper["routing_matches"] = {}
        paper["routing_reason"] = "excluded by hard rule"
        return paper

    scored: list[tuple[str, float, list[str]]] = []
    for key, cfg in directions.items():
        score, matched = score_direction(text, cfg)
        if score >= min_score:
            scored.append((key, score, matched))
    scored.sort(key=lambda item: item[1], reverse=True)

    paper["directions"] = [key for key, _, _ in scored]
    paper["direction"] = scored[0][0] if scored else None
    paper["direction_name"] = (
        directions[paper["direction"]].get("display_name") if paper["direction"] else None
    )
    paper["routing_matches"] = {key: matched for key, _, matched in scored}
    paper.pop("routing_reason", None)
    return paper


def filter_routed(papers: list[dict]) -> list[dict]:
    return [p for p in papers if p.get("direction")]


def crossovers(paper: dict, pairs: list[tuple[str, str, str]]) -> list[list[str]]:
    """The configured pairs this paper's directions satisfy, in config order."""
    held = set(paper.get("directions") or [])
    return [[a, b] for a, b, _ in pairs if a in held and b in held]


def apply_crossover_boost(papers: list[dict], pairs: list[tuple[str, str, str]]) -> int:
    """Bump priority one level for papers that sit on a configured pair.

    One bump per paper however many pairs it satisfies; idempotent. Writes
    ``priority_pre_boost``, ``priority_boosted`` and ``boost_reason`` into
    ``llm`` exactly as v1 did, so the site needs no new field. Returns the
    number of papers boosted.
    """
    boosted = 0
    for paper in papers:
        hits = [(a, b, reason) for a, b, reason in pairs
                if {a, b} <= set(paper.get("directions") or [])]
        if not hits:
            continue
        llm = paper.get("llm") or {}
        if llm.get("priority_boosted"):
            continue
        priority = llm.get("priority")
        if priority not in BUMP:
            continue
        a, b, reason = hits[0]
        llm["priority_pre_boost"] = priority
        llm["priority"] = BUMP[priority]
        llm["priority_boosted"] = True
        llm["boost_reason"] = reason or f"{a} × {b} crossover"
        paper["llm"] = llm
        boosted += 1
    return boosted
