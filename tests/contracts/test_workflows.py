"""Every workflow that writes to the repository follows the same contract.

A "writer" job is one with a step that runs ``git commit`` or the push
retry script. v1 lost a backfill to a cancelled commit step and a published
site to a hand-maintained trigger list; these assertions are the memory.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

WORKFLOWS = sorted((pathlib.Path(__file__).resolve().parents[2] / ".github" / "workflows").glob("*.yml"))


def _load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML parses the bare `on:` key as boolean True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def _steps(job):
    return job.get("steps") or []


def _is_writer(job) -> bool:
    text = " ".join(str(s.get("run", "")) for s in _steps(job))
    return "git commit" in text or "git_push_retry" in text


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_workflow_parses_and_has_minimal_permissions(path):
    data = _load(path)
    assert data.get("jobs"), f"{path.name}: no jobs"
    assert "permissions" in data or all("permissions" in j for j in data["jobs"].values()), \
        f"{path.name}: declare permissions explicitly"


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_cron_minute_is_not_zero(path):
    data = _load(path)
    schedules = (data["on"] or {}).get("schedule") or [] if isinstance(data["on"], dict) else []
    for entry in schedules:
        minute = str(entry["cron"]).split()[0]
        assert minute not in ("0", "*"), f"{path.name}: cron {entry['cron']} fires on the hour (GitHub drops those)"


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_writer_jobs_follow_the_contract(path):
    data = _load(path)
    for name, job in data["jobs"].items():
        if not _is_writer(job):
            continue
        label = f"{path.name}:{name}"
        conc = job.get("concurrency") or data.get("concurrency")
        assert conc and conc.get("group") == "radar-writer", f"{label}: concurrency group radar-writer"
        assert conc.get("cancel-in-progress") is False, f"{label}: cancel-in-progress must be false"
        checkout = next((s for s in _steps(job) if str(s.get("uses", "")).startswith("actions/checkout")), None)
        assert checkout and (checkout.get("with") or {}).get("fetch-depth") == 1, f"{label}: fetch-depth: 1"
        for step in _steps(job):
            run = str(step.get("run", ""))
            if "git commit" in run:
                assert "!cancelled()" in str(step.get("if", "")), f"{label}: commit step needs if: !cancelled()"
                assert "git_push_retry" in run or any("git_push_retry" in str(s.get("run", "")) for s in _steps(job)), \
                    f"{label}: push through scripts/git_push_retry.sh"
            assert not re.search(r"\bgit push\b", run) or "git_push_retry" in run, f"{label}: bare git push"


def test_tests_workflow_exists_and_ignores_data_pushes():
    path = next((p for p in WORKFLOWS if p.name == "tests.yml"), None)
    assert path, "tests.yml missing"
    data = _load(path)
    on = data["on"]
    assert "pull_request" in on
    assert "data/**" in (on.get("push") or {}).get("paths-ignore", [])
    runs = " ".join(str(s.get("run", "")) for j in data["jobs"].values() for s in _steps(j))
    assert 'pip install -e ".[dev]"' in runs and "pytest" in runs
