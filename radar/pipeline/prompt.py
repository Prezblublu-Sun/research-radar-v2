"""Build what the scorer actually sends.

v1 loaded ``prompts/scorer_v3.txt`` and sent it verbatim — the system prompt
began with the literal text ``{direction_context}`` and ended with
``Title: {title}`` because nothing ever substituted them. The direction
focus and the paper reached the model only through the user message, so
scoring worked; the model was simply shown a template.

Here the system prompt is rendered once per run (``str.replace``, not
``str.format`` — the JSON schema in the prompt is full of braces) and the
user message carries the routed direction's focus questions and the paper.
That order — identical system prompt, then one of six direction blocks,
then the paper — is what makes DeepSeek's prefix cache hit on ~75% of
prompt tokens; keep it.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from dataclasses import dataclass

from radar.config import Directions
from radar.core.records import ABSTRACT_MAX_CHARS

PLACEHOLDER = "{direction_context}"
_VERSION = re.compile(r"^scorer_(v\d+)\.txt$")


@dataclass(frozen=True)
class PromptBundle:
    file: str
    text: str
    sha: str              # "sha256:<16 hex>" of the file text
    version: str          # "v4" from "scorer_v4.txt"


def load(prompts_dir: pathlib.Path, file: str) -> PromptBundle:
    path = pathlib.Path(prompts_dir) / file
    text = path.read_text(encoding="utf-8")
    match = _VERSION.match(file)
    return PromptBundle(
        file=file, text=text,
        sha="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        version=match.group(1) if match else "unknown",
    )


def direction_context(config: Directions) -> str:
    lines = [f"- {key} — {config.display_name(key)}" for key in config.keys]
    pairs = config.crossover_pairs
    if pairs:
        anchors = sorted({b for _, b, _ in pairs} & set(config.keys))
        methods = sorted({a for a, _, _ in pairs} - set(anchors))
        if anchors and methods:
            lines.append("")
            lines.append(
                "The researcher's own work sits at the intersection of "
                + ", ".join(config.display_name(a) for a in anchors)
                + " with the method directions ("
                + ", ".join(config.display_name(m) for m in methods)
                + "). A paper that brings one of those methods to that domain "
                "is the most valuable kind."
            )
    return "\n".join(lines)


def system_prompt(bundle: PromptBundle, config: Directions) -> str:
    return bundle.text.replace(PLACEHOLDER, direction_context(config))


def user_message(paper: dict, config: Directions,
                 crossover: list[list[str]] | None = None) -> str:
    key = paper.get("direction") or ""
    lines = [
        f"Direction this paper was routed to: {config.display_name(key) if key else 'unknown'}",
        "",
        "Direction-specific evaluation focus:",
        config.focus(key).rstrip() or "(none)",
    ]
    if crossover:
        others = sorted({d for pair in crossover for d in pair} - {key})
        reasons = [reason for a, b, reason in config.crossover_pairs
                   if [a, b] in crossover or [b, a] in crossover]
        lines += [
            "",
            "Crossover: this paper also matched "
            + ", ".join(config.display_name(d) for d in others)
            + ". In relevance_to_user, say whether it actually brings "
            + (reasons[0] if reasons and reasons[0] else "that method to the domain")
            + ".",
        ]
    authors = paper.get("authors") or []
    author_text = ", ".join(str(a) for a in authors[:6]) + (" et al." if len(authors) > 6 else "")
    lines += [
        "",
        "Paper:",
        f"Title: {paper.get('title', '')}",
        f"Authors: {author_text}",
        f"Venue: {paper.get('venue', '')}",
        f"Date: {paper.get('date', '')}",
        f"Citations: {paper.get('cited_by_count', 0)}",
        f"Abstract: {(paper.get('abstract') or '')[:ABSTRACT_MAX_CHARS]}",
        "",
        "Output JSON only.",
    ]
    return "\n".join(lines)
