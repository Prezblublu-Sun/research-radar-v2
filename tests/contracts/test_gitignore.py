"""Derived directories must never reach a commit.

Phase 2's first commit swept in a 4.4 MB dry-run file because the ignore
patterns carried trailing comments, which git reads as part of the pattern.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
MUST_IGNORE = [".radar-cache/seen_keys.json", ".radar-dryrun/data/runs/2026/x.jsonl",
               "_site/index.html", ".env", ".venv/lib/x.py"]
MUST_TRACK = ["data/runs/2026/x.jsonl", "data/random_reading/2026/x.jsonl", "config/directions.yaml"]


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
@pytest.mark.parametrize("path", MUST_IGNORE)
def test_derived_paths_are_ignored(path):
    code = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO).returncode
    assert code == 0, f"{path} is not ignored"


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
@pytest.mark.parametrize("path", MUST_TRACK)
def test_data_paths_are_not_ignored(path):
    code = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO).returncode
    assert code == 1, f"{path} would be ignored"


def test_no_pattern_line_carries_a_trailing_comment():
    for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert " #" not in stripped, f"trailing comment in pattern: {line!r}"
