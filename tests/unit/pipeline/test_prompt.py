"""The scorer must see a rendered prompt, not the template (v1 sent the
literal ``{direction_context}`` for five months)."""
from __future__ import annotations

import re

from radar.pipeline import prompt


def test_system_prompt_renders_every_direction_and_no_placeholder(prompt_bundle, cfg):
    text = prompt.system_prompt(prompt_bundle, cfg)
    assert prompt.PLACEHOLDER not in text
    for key in cfg.keys:
        assert key in text and cfg.display_name(key) in text
    assert not re.search(r"\{(title|abstract|authors|venue|date|doi)\}", text)


def test_system_prompt_keeps_the_v3_output_schema(prompt_bundle, repo_root):
    v3 = (repo_root / "prompts" / "scorer_v3.txt").read_text(encoding="utf-8")
    schema = v3[v3.index("Schema:"):v3.index("Priority rules:")]
    assert schema in prompt_bundle.text


def test_system_prompt_is_identical_across_papers_for_prefix_cache(prompt_bundle, cfg):
    # One system prompt per run → the provider's prefix cache hits.
    assert prompt.system_prompt(prompt_bundle, cfg) == prompt.system_prompt(prompt_bundle, cfg)


def test_user_message_carries_focus_crossover_and_paper(cfg):
    key = cfg.keys[0]
    paper = {"direction": key, "title": "T", "authors": ["A", "B"], "venue": "V",
             "date": "2026-09-01", "cited_by_count": 3, "abstract": "x" * 5000}
    msg = prompt.user_message(paper, cfg, crossover=[[cfg.keys[0], cfg.keys[-1]]])
    assert cfg.display_name(key) in msg
    assert cfg.focus(key).strip()[:40] in msg
    assert "Crossover" in msg and cfg.display_name(cfg.keys[-1]) in msg
    assert "Title: T" in msg and "Authors: A, B" in msg
    assert len(msg) < 5000  # abstract capped at 3000


def test_bundle_version_and_sha(repo_root):
    bundle = prompt.load(repo_root / "prompts", "scorer_v4.txt")
    assert bundle.version == "v4"
    assert bundle.sha.startswith("sha256:") and len(bundle.sha) == len("sha256:") + 16
