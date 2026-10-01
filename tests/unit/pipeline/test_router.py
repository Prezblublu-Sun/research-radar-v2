"""Keyword routing against the live config: plural tolerance, pair-only
terms, exclusions with anchors, and the config-driven crossover boost."""
from __future__ import annotations

import pytest

from radar.pipeline import router


def _route(cfg, title, abstract=""):
    paper = {"title": title, "abstract": abstract}
    router.route(paper, cfg.directions, cfg.exclusions)
    return paper


# --- term matching ---------------------------------------------------------

@pytest.mark.parametrize("text, term, hit", [
    ("fourier neural operators on meshes", "Fourier neural operator", True),
    ("we train pinns", "PINN", True),
    ("pinnacle of success", "PINN", False),          # not a prefix match
    ("spinning", "PINN", False),                     # word boundary kept
    ("multi-fidelity data", "multi-fidelity", True),
    ("neural operator", "neural operators", False),  # only a trailing s is tolerated
])
def test_contains_tolerates_plurals_but_keeps_word_boundaries(text, term, hit):
    assert router.contains(text, term) is hit


# --- routing ---------------------------------------------------------------

def test_plural_neural_operators_route_to_geo_operator(cfg):
    p = _route(cfg, "Neural operators for mesh-based field prediction",
               "We learn neural operators that predict full fields on unstructured meshes.")
    assert p["direction"] == "geo_operator"
    assert "geo_operator" in p["routing_matches"]


def test_pinns_route_to_fe_physics_informed(cfg):
    p = _route(cfg, "PINNs for hyperelastic solids",
               "Physics-informed neural networks solve the hyperelastic boundary value problem.")
    assert "fe_physics_informed" in p["directions"]


def test_generic_rl_does_not_route_anywhere(cfg):
    # Reinforcement learning is pair-only: 977 bare-word papers were 89% Low/Exclude.
    p = _route(cfg, "Deep reinforcement learning for Atari games",
               "We train an agent to play Atari with reinforcement learning and report scores on ALE.")
    assert p["direction"] is None and p["directions"] == []


def test_rl_paired_with_finite_element_routes_to_design_loop(cfg):
    p = _route(cfg, "Reinforcement learning controller trained in a finite element environment",
               "The policy interacts with a finite element solver of a beam.")
    assert p["direction"] == "design_loop"
    assert "reinforcement learning + finite element" in p["routing_matches"]["design_loop"]


def test_femoral_stem_paper_routes_to_stem_biomech_first(cfg):
    p = _route(cfg, "Stress shielding of a short femoral stem",
               "A finite element study of the femoral stem and bone remodelling.")
    assert p["direction"] == "stem_biomech"
    assert p["direction_name"] == cfg.display_name("stem_biomech")


def test_primary_direction_is_the_highest_score(cfg):
    p = _route(cfg, "Gaussian process surrogate with uncertainty quantification for femoral stem stress",
               "A Gaussian process surrogate of finite element stress shielding with calibrated uncertainty "
               "and reliability analysis for the stem.")
    assert p["directions"][0] == p["direction"]
    assert set(p["directions"]) >= {"stem_biomech", "surrogate_uq"}


# --- exclusions ------------------------------------------------------------

def test_hard_exclusion_without_anchor(cfg):
    p = _route(cfg, "Nanoparticle drug delivery for tumours", "A drug delivery system for cancer therapy.")
    assert p["direction"] is None and p["routing_reason"] == "excluded by hard rule"


def test_hard_exclusion_term_is_ignored_when_an_anchor_is_present(cfg):
    p = _route(cfg, "Drug delivery coating on a porous titanium implant",
               "Osseointegration of a porous implant with an antibiotic drug delivery coating.")
    assert "routing_reason" not in p
    assert p["direction"] == "stem_biomech"


def test_route_clears_a_stale_routing_reason(cfg):
    paper = {"title": "Neural operators for finite element stress", "abstract": "", "routing_reason": "old"}
    router.route(paper, cfg.directions, cfg.exclusions)
    assert "routing_reason" not in paper


# --- crossover boost -------------------------------------------------------

def _crossover_paper(priority, directions=("stem_biomech", "surrogate_uq")):
    return {"directions": list(directions), "llm": {"priority": priority}}


def test_crossovers_lists_configured_pairs_in_config_order(cfg):
    paper = {"directions": ["surrogate_uq", "stem_biomech", "geo_operator"]}
    assert router.crossovers(paper, cfg.crossover_pairs) == [["stem_biomech", "geo_operator"],
                                                             ["stem_biomech", "surrogate_uq"]]
    assert router.crossovers({"directions": ["geo_operator", "surrogate_uq"]}, cfg.crossover_pairs) == []


def test_boost_low_becomes_medium_with_reason_from_config(cfg):
    p = _crossover_paper("Low")
    assert router.apply_crossover_boost([p], cfg.crossover_pairs) == 1
    assert p["llm"]["priority"] == "Medium"
    assert p["llm"]["priority_pre_boost"] == "Low"
    assert p["llm"]["priority_boosted"] is True
    assert p["llm"]["boost_reason"] == "uncertainty-aware implant biomechanics"


def test_boost_medium_becomes_high(cfg):
    p = _crossover_paper("Medium")
    assert router.apply_crossover_boost([p], cfg.crossover_pairs) == 1
    assert p["llm"]["priority"] == "High"


@pytest.mark.parametrize("priority", ["High", "Exclude", None])
def test_boost_leaves_high_exclude_and_unscored_alone(cfg, priority):
    p = _crossover_paper(priority)
    assert router.apply_crossover_boost([p], cfg.crossover_pairs) == 0
    assert p["llm"]["priority"] == priority
    assert "priority_boosted" not in p["llm"]


def test_boost_skips_single_direction_and_unconfigured_pairs(cfg):
    single = _crossover_paper("Low", ("stem_biomech",))
    methods_only = _crossover_paper("Low", ("geo_operator", "surrogate_uq"))
    assert router.apply_crossover_boost([single, methods_only], cfg.crossover_pairs) == 0
    assert single["llm"]["priority"] == methods_only["llm"]["priority"] == "Low"


def test_boost_is_idempotent_and_bumps_once_for_many_pairs(cfg):
    p = _crossover_paper("Low", ("stem_biomech", "surrogate_uq", "geo_operator", "design_loop"))
    router.apply_crossover_boost([p], cfg.crossover_pairs)
    assert p["llm"]["priority"] == "Medium"
    assert router.apply_crossover_boost([p], cfg.crossover_pairs) == 0
    assert p["llm"]["priority"] == "Medium"
