from __future__ import annotations

import copy

import pytest
import yaml

from radar import config


def test_live_config_loads_with_six_directions_and_unions(cfg):
    assert len(cfg.keys) == 6
    assert cfg.crossover, "crossover_boost pairs are part of the product"
    for a, b, _ in cfg.crossover_pairs:
        assert a in cfg.keys and b in cfg.keys
    assert cfg.openalex_concepts == sorted(set(cfg.openalex_concepts))
    assert all(c.startswith("C") for c in cfg.openalex_concepts)
    assert cfg.arxiv_categories and cfg.openalex_keywords and cfg.pubmed_terms
    for key in cfg.keys:
        assert cfg.focus(key).strip()
        assert cfg.display_name(key) != key


def _raw(repo_root):
    return yaml.safe_load((repo_root / "config" / "directions.yaml").read_text(encoding="utf-8"))


def test_validate_reports_every_problem_at_once(repo_root):
    raw = copy.deepcopy(_raw(repo_root))
    first = next(iter(raw["directions"]))
    raw["directions"][first].pop("display_name")
    raw["directions"][first]["sources"]["openalex_concepts"] = ["not-an-id"]
    raw["directions"]["Bad Key"] = {"display_name": "x", "llm_prompt_focus": "y"}
    raw["crossover_boost"].append({"pair": ["ghost", first]})
    problems = config.validate(raw)
    joined = "\n".join(problems)
    assert "display_name is required" in joined
    assert "not-an-id" in joined
    assert "snake_case" in joined
    assert "can never route" in joined
    assert "ghost" in joined


def test_load_raises_config_error_with_path(tmp_path):
    path = tmp_path / "directions.yaml"
    path.write_text("directions: {}\n", encoding="utf-8")
    with pytest.raises(config.ConfigError) as caught:
        config.load(path)
    assert "directions.yaml" in str(caught.value)


def test_sha_changes_with_content(tmp_path, repo_root):
    text = (repo_root / "config" / "directions.yaml").read_text(encoding="utf-8")
    a = tmp_path / "a.yaml"; a.write_text(text, encoding="utf-8")
    b = tmp_path / "b.yaml"; b.write_text(text + "\n# comment\n", encoding="utf-8")
    assert config.load(a).sha != config.load(b).sha
