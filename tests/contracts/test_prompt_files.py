"""Scorer prompts are versioned and never edited in place.

A change to a prompt is a new ``scorer_vN+1.txt``; the pinned hashes make
an in-place edit fail CI. Add the new file's hash here when you add it.
"""
from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

PROMPTS = pathlib.Path(__file__).resolve().parents[2] / "prompts"
PINNED = {
    # frozen v1 prompt; reproduces the provenance of imported v1 records
    "scorer_v3.txt": "9e25f089ebc378efc530bca0cd2d08cba3440e89f971ff8b5b14091efaf19935",
    # v2 active prompt: six directions, {direction_context} rendered, no dead metadata block
    "scorer_v4.txt": "8816a29e74e19ee2a1ab13b681a1d9939a2802bc0a332aa6a8eb8898beff4540",
}


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("name, digest", sorted(PINNED.items()))
def test_pinned_prompt_is_unchanged(name, digest):
    assert _sha(PROMPTS / name) == digest, f"{name} was edited in place; create scorer_vN+1.txt instead"


def test_every_shipped_prompt_is_pinned():
    shipped = {p.name for p in PROMPTS.glob("scorer_v*.txt")}
    assert shipped == set(PINNED), f"unpinned prompt files: {sorted(shipped - set(PINNED))}"
    assert all(re.match(r"^scorer_v\d+\.txt$", n) for n in shipped)


def test_active_prompt_has_one_placeholder_and_no_dead_metadata_block():
    text = (PROMPTS / "scorer_v4.txt").read_text(encoding="utf-8")
    assert text.count("{direction_context}") == 1
    assert "{title}" not in text and "{abstract}" not in text
