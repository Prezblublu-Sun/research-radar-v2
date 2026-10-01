"""Load and validate ``config/directions.yaml``.

The YAML is the product: six research directions, each with the terms that
drive fetching (arXiv categories, OpenAlex concepts and keywords, PubMed
terms), the keyword rules that drive routing, and the focus questions the
scorer asks. Plus the crossover pairs that earn a priority bump and the
global exclusions.

v1 absorbed this into the orchestrator's first forty lines and validated
nothing; a misspelt key routed zero papers in silence.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass, field

import yaml

_CONCEPT_ID = re.compile(r"^C\d+$")


class ConfigError(ValueError):
    """directions.yaml is not usable; the message lists every problem found."""


@dataclass(frozen=True)
class CrossoverPair:
    a: str
    b: str
    reason: str


@dataclass(frozen=True)
class Directions:
    path: pathlib.Path
    sha: str                                  # "sha256:<16 hex>" of the file text
    directions: dict[str, dict]               # key -> raw mapping, insertion order
    exclusions: dict
    crossover: tuple[CrossoverPair, ...] = ()

    # --- lookups ---------------------------------------------------------------
    @property
    def keys(self) -> list[str]:
        return list(self.directions)

    def display_name(self, key: str) -> str:
        return str(self.directions.get(key, {}).get("display_name") or key)

    def color(self, key: str) -> str:
        return str(self.directions.get(key, {}).get("color") or "#8B8980")

    def focus(self, key: str) -> str:
        return str(self.directions.get(key, {}).get("llm_prompt_focus") or "")

    @property
    def crossover_pairs(self) -> list[tuple[str, str, str]]:
        return [(p.a, p.b, p.reason) for p in self.crossover]

    # --- unions that drive the fetchers ----------------------------------------
    def _union(self, section: str) -> list[str]:
        seen: list[str] = []
        for cfg in self.directions.values():
            for term in (cfg.get("sources") or {}).get(section) or []:
                if term not in seen:
                    seen.append(term)
        return seen

    @property
    def arxiv_categories(self) -> list[str]:
        return sorted(self._union("arxiv_categories"))

    @property
    def openalex_concepts(self) -> list[str]:
        return sorted(self._union("openalex_concepts"))

    @property
    def openalex_keywords(self) -> list[str]:
        return sorted(self._union("openalex_keywords"))

    @property
    def pubmed_terms(self) -> list[str]:
        return sorted(self._union("pubmed_terms"))


def _sha(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def validate(raw: dict) -> list[str]:
    """Every problem found, as human sentences. Empty means valid."""
    problems: list[str] = []
    directions = raw.get("directions")
    if not isinstance(directions, dict) or not directions:
        return ["`directions:` is missing or empty"]
    for key, cfg in directions.items():
        if not re.match(r"^[a-z][a-z0-9_]*$", str(key)):
            problems.append(f"direction key {key!r} must be snake_case")
        if not isinstance(cfg, dict):
            problems.append(f"{key}: must be a mapping")
            continue
        if not cfg.get("display_name"):
            problems.append(f"{key}: display_name is required")
        if not cfg.get("llm_prompt_focus", "").strip():
            problems.append(f"{key}: llm_prompt_focus is required (the scorer asks it)")
        strong = cfg.get("strong_keywords") or []
        pairs = cfg.get("must_pair_with") or []
        if not strong and not pairs:
            problems.append(f"{key}: no strong_keywords and no must_pair_with — it can never route")
        for term in strong:
            if not isinstance(term, str) or not term.strip():
                problems.append(f"{key}: strong_keywords contains a blank or non-string entry")
                break
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(t, str) and t.strip() for t in pair)):
                problems.append(f"{key}: must_pair_with entry {pair!r} is not two terms")
        for cid in (cfg.get("sources") or {}).get("openalex_concepts") or []:
            if not _CONCEPT_ID.match(str(cid)):
                problems.append(f"{key}: openalex concept {cid!r} is not a C<digits> id")
    for pair in raw.get("crossover_boost") or []:
        names = pair.get("pair") if isinstance(pair, dict) else None
        if not (isinstance(names, list) and len(names) == 2):
            problems.append(f"crossover_boost entry {pair!r} needs `pair: [a, b]`")
            continue
        for name in names:
            if name not in directions:
                problems.append(f"crossover_boost names unknown direction {name!r}")
        if names[0] == names[1]:
            problems.append(f"crossover_boost pair {names!r} pairs a direction with itself")
    return problems


def load(path) -> Directions:
    path = pathlib.Path(path)
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    problems = validate(raw)
    if problems:
        raise ConfigError(f"{path}:\n  - " + "\n  - ".join(problems))
    crossover = tuple(
        CrossoverPair(entry["pair"][0], entry["pair"][1], str(entry.get("reason") or ""))
        for entry in raw.get("crossover_boost") or []
    )
    return Directions(
        path=path,
        sha=_sha(text),
        directions=dict(raw["directions"]),
        exclusions=dict(raw.get("exclusions") or {}),
        crossover=crossover,
    )
