"""``radar doctor``: can this checkout run, and is the data it holds sane?

Offline and under two seconds. Every finding is a line; the exit code is
non-zero only for problems that would break a run, warnings never fail it.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import re
from dataclasses import dataclass, field

import yaml

from radar import config as _config
from radar.paths import DataRoot
from radar.pipeline import prompt as _prompt
from radar.store import runs as _runs

ENV_REQUIRED_FOR_SCORING = ("OPENAI_API_KEY",)
ENV_OPTIONAL = ("OPENAI_BASE_URL", "MODEL_NAME", "OPENALEX_API_KEY", "OPENALEX_EMAIL", "PUBMED_EMAIL")


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def check(repo: pathlib.Path, data_root: DataRoot, prompt_file: str) -> Report:
    report = Report()

    # config
    try:
        cfg = _config.load(repo / "config" / "directions.yaml")
        report.notes.append(f"config: {len(cfg.keys)} directions, {len(cfg.crossover)} crossover pairs, {cfg.sha}")
    except Exception as error:  # noqa: BLE001
        report.problems.append(f"config: {error}")
        cfg = None

    # prompt
    prompt_path = repo / "prompts" / prompt_file
    if not prompt_path.exists():
        report.problems.append(f"prompt: {prompt_path} does not exist")
    else:
        text = prompt_path.read_text(encoding="utf-8")
        n = text.count(_prompt.PLACEHOLDER)
        if n != 1:
            report.problems.append(f"prompt: {prompt_file} contains {_prompt.PLACEHOLDER} {n} times (need exactly 1)")
        for leftover in re.findall(r"\{(title|abstract|authors|venue|date|doi)\}", text):
            report.problems.append(f"prompt: {prompt_file} still has the unfilled v1 placeholder {{{leftover}}}")
        report.notes.append(f"prompt: {prompt_file}")

    # workflows (structure only; the contract tests go deeper)
    wf_dir = repo / ".github" / "workflows"
    for path in sorted(wf_dir.glob("*.yml")) if wf_dir.is_dir() else []:
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            report.problems.append(f"workflow {path.name}: {error}")

    # data
    files = _runs.list_runs(data_root)
    if not files:
        report.warnings.append("data: no run logs yet")
    else:
        newest = files[-1]
        try:
            header = _runs.read_header(newest)
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
                header.get("finished_at", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
            report.notes.append(f"data: {len(files)} run logs, newest {newest.name} "
                                f"({header.get('run_status')}, {age.total_seconds() / 3600:.0f} h ago)")
            if age > dt.timedelta(hours=36) and header.get("run_type") == "daily":
                report.warnings.append(f"data: newest daily run is {age.days} days old")
            for flag in header.get("quality_flags") or []:
                report.warnings.append(f"data: newest run carries {flag}")
        except Exception as error:  # noqa: BLE001
            report.problems.append(f"data: {newest.name} is not a readable run log: {error}")

    # environment (never a problem: dry runs need none of it)
    for name in ENV_REQUIRED_FOR_SCORING:
        if not os.environ.get(name):
            report.warnings.append(f"env: {name} not set (scoring will refuse; --dry-run works)")
    return report


def render(report: Report) -> str:
    lines = []
    for line in report.problems:
        lines.append(f"PROBLEM  {line}")
    for line in report.warnings:
        lines.append(f"warning  {line}")
    for line in report.notes:
        lines.append(f"ok       {line}")
    lines.append("doctor: " + ("OK" if report.ok else f"{len(report.problems)} problem(s)"))
    return "\n".join(lines)
