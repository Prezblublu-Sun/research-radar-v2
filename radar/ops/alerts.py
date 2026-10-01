"""Alerts: one standing GitHub issue, edited silently, commented on failure.

The site is static and the repository holds no mail credential, so the
notification path is GitHub's own (the same mechanism as the 待读 digest):
an issue labelled ``radar-alerts`` assigned to the owner. Its *body* is
rewritten after every run with the current state (editing a body sends no
mail); a *comment* is posted only when a job failed or the health verdict
blocked, and the comment is the email. Warnings go to the body only.

This module renders the Markdown; the workflow does the ``gh issue`` calls.
Every value interpolated comes from run headers the pipeline wrote itself,
but source error messages echo remote responses, so each field is flattened
and truncated before it reaches the issue.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field

from radar.paths import DataRoot
from radar.pipeline import health as _health
from radar.store import runs as _runs

LABEL = "radar-alerts"
ISSUE_TITLE = "Radar 运行报警"
RECENT_RUNS = 7
_FLAT = re.compile(r"[\r\n`|]+")


@dataclass
class Alert:
    body: str
    comment: str | None
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def alerting(self) -> bool:
        return self.comment is not None


def plain(value, limit: int = 160) -> str:
    text = _FLAT.sub(" ", str(value if value is not None else "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def recent_headers(root: DataRoot, limit: int = RECENT_RUNS) -> list[dict]:
    """Newest finished first; an unreadable file is reported as a pseudo-header."""
    headers: list[dict] = []
    for path in _runs.list_runs(root):
        try:
            headers.append(_runs.read_header(path))
        except Exception as error:  # noqa: BLE001
            headers.append({"run_id": path.stem, "run_status": "unreadable", "finished_at": "9999",
                            "quality_flags": [f"unreadable: {type(error).__name__}"], "counts": {}})
    headers.sort(key=_runs.run_order, reverse=True)
    return headers[:limit]


def _age_hours(header: dict, now: dt.datetime) -> float | None:
    stamp = header.get("finished_at") or header.get("started_at")
    if not stamp:
        return None
    try:
        finished = dt.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - finished).total_seconds() / 3600


def _row(header: dict) -> str:
    counts = header.get("counts") or {}
    prio = counts.get("priority_counts") or {}
    llm = header.get("llm") or {}
    return "| {run} | {typ} | {status} | {fetched} | {routed} | {hm} | {usd} | {flags} |".format(
        run=plain(header.get("run_id"), 24), typ=plain(header.get("run_type"), 10),
        status=plain(header.get("run_status"), 16),
        fetched=plain(counts.get("fetched", ""), 8), routed=plain(counts.get("after_routing", ""), 8),
        hm=f"{prio.get('High', 0)}/{prio.get('Medium', 0)}" if prio else "",
        usd=plain(llm.get("estimated_usd", ""), 10),
        flags=plain(", ".join(header.get("quality_flags") or []), 80) or "—")


def render(root: DataRoot, *, job: str, conclusion: str, run_url: str,
           now: dt.datetime | None = None, max_age_hours: float = 30.0,
           publish_conclusion: str = "") -> Alert:
    now = now or dt.datetime.now(dt.timezone.utc)
    headers = recent_headers(root)
    latest = headers[0] if headers else None
    verdict = _health.evaluate(latest) if latest else _health.Verdict(blocking=["没有任何运行记录"])

    reasons: list[str] = []
    if conclusion != "success":
        reasons.append(f"工作流任务 `{plain(job, 40)}` 结束状态为 **{plain(conclusion, 20)}**（通常是某一步抛错，见运行日志）")
    if publish_conclusion in ("failure", "cancelled", "timed_out"):
        reasons.append(f"站点发布任务结束状态为 **{plain(publish_conclusion, 20)}**（数据已提交，但 Pages 没有更新）")
    reasons += [plain(line) for line in verdict.blocking]
    age = _age_hours(latest, now) if latest else None
    if latest and age is not None and age > max_age_hours:
        reasons.append(f"最近一次运行记录已是 {age:.0f} 小时前（{plain(latest.get('run_id'), 24)}）")

    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# {ISSUE_TITLE}", "", f"更新于 {stamp}。本 issue 的正文每次运行后静默重写；只有运行失败或健康门拦截时才会追加评论（评论即邮件）。", ""]
    if latest:
        lines += [f"**最近一次运行**：`{plain(latest.get('run_id'), 24)}`（{plain(latest.get('run_type'), 10)}），"
                  f"状态 **{plain(latest.get('run_status'), 16)}**"
                  + (f"，{age:.0f} 小时前" if age is not None else ""), ""]
    else:
        lines += ["**最近一次运行**：无记录", ""]
    lines += ["## 最近运行", "", "| run_id | 类型 | 状态 | 抓取 | 路由 | High/Med | 估算 USD | 质量标记 |",
              "|---|---|---|---|---|---|---|---|"]
    lines += [_row(h) for h in headers] or ["| — | | | | | | | |"]
    lines += ["", "## 当前警告", ""]
    lines += [f"- {plain(w)}" for w in verdict.warnings] or ["- 无"]
    if reasons:
        lines += ["", "## 当前报警", ""] + [f"- {r}" for r in reasons]
    lines += ["", f"运行链接：{plain(run_url, 200)}" if run_url else ""]
    body = "\n".join(lines).rstrip() + "\n"

    comment = None
    if reasons:
        comment = "\n".join(
            [f"**{plain(job, 40)} 失败** — {stamp}", ""] + [f"- {r}" for r in reasons]
            + ["", f"运行日志：{plain(run_url, 200)}" if run_url else ""]).rstrip() + "\n"

    outputs = {
        "alert": "true" if comment else "false",
        "latest_run_id": plain(latest.get("run_id"), 24) if latest else "",
        "latest_status": plain(latest.get("run_status"), 16) if latest else "none",
    }
    return Alert(body=body, comment=comment, outputs=outputs)


def write_files(alert: Alert, out_dir: pathlib.Path, github_output: pathlib.Path | None = None) -> None:
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "alert-body.md").write_text(alert.body, encoding="utf-8")
    comment_path = out_dir / "alert-comment.md"
    if alert.comment:
        comment_path.write_text(alert.comment, encoding="utf-8")
    elif comment_path.exists():
        comment_path.unlink()
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            for key, value in alert.outputs.items():
                handle.write(f"{key}={value}\n")
