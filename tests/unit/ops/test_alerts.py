"""Alert rendering: body always, comment only on failure or a blocked gate."""
from __future__ import annotations

import datetime as dt

from radar.core.atomic import write_jsonl_new
from radar.ops import alerts

NOW = dt.datetime(2026, 10, 2, 13, 0, tzinfo=dt.timezone.utc)


def _run(root, run_id, *, status="success", flags=(), finished="2026-10-02T12:30:00Z", counts=None, run_type="daily"):
    header = {"kind": "run", "run_id": run_id, "run_type": run_type, "run_status": status,
              "quality_flags": list(flags), "finished_at": finished,
              "counts": counts or {"fetched": 300, "after_routing": 40, "priority_counts": {"High": 2, "Medium": 9},
                                   "scorer_failed": 0},
              "llm": {"estimated_usd": 0.0712}}
    write_jsonl_new(root.run_file(run_id, run_type), [header, {"ck": "doi:a", "kind": "paper"}])


def test_success_renders_body_only(data_root):
    _run(data_root, "2026-10-02T121700Z")
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="https://x/1", now=NOW)
    assert not alert.alerting and alert.comment is None
    assert "2026-10-02T121700Z" in alert.body and "| 2/9 |" in alert.body and "0.0712" in alert.body
    assert alert.outputs == {"alert": "false", "latest_run_id": "2026-10-02T121700Z", "latest_status": "success"}


def test_warnings_go_to_the_body_not_a_comment(data_root):
    _run(data_root, "2026-10-02T121700Z", status="partial_success", flags=["arxiv_failed", "openalex_truncated"])
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW)
    assert not alert.alerting
    assert "arxiv_failed" in alert.body and "## 当前警告" in alert.body


def test_job_failure_comments_with_the_run_url(data_root):
    _run(data_root, "2026-10-02T121700Z")
    alert = alerts.render(data_root, job="run", conclusion="failure", run_url="https://x/2", now=NOW)
    assert alert.alerting
    assert "failure" in alert.comment and "https://x/2" in alert.comment
    assert alert.outputs["alert"] == "true"


def test_failed_publish_is_an_alert_even_when_the_run_succeeded(data_root):
    _run(data_root, "2026-10-02T121700Z")
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW, publish_conclusion="failure")
    assert alert.alerting and "Pages 没有更新" in alert.comment
    skipped = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW, publish_conclusion="skipped")
    assert not skipped.alerting


def test_blocked_gate_comments_with_the_reason(data_root):
    _run(data_root, "2026-10-02T121700Z", status="failed", flags=["fetched_zero"])
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW)
    assert alert.alerting and "fetched_zero" in alert.comment


def test_stale_newest_run_is_an_alert(data_root):
    _run(data_root, "2026-09-30T121700Z", finished="2026-09-30T12:30:00Z")
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW)
    assert alert.alerting and "小时前" in alert.comment


def test_no_runs_at_all_is_an_alert(data_root):
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW)
    assert alert.alerting and "没有任何运行记录" in alert.comment
    assert alert.outputs["latest_status"] == "none"


def test_table_lists_newest_first_and_at_most_seven(data_root):
    for day in range(1, 10):
        _run(data_root, f"2026-10-0{day}T121700Z", finished=f"2026-10-0{day}T12:30:00Z")
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW.replace(day=9))
    rows = [l for l in alert.body.splitlines() if l.startswith("| 2026-")]
    assert len(rows) == 7 and rows[0].startswith("| 2026-10-09") and rows[-1].startswith("| 2026-10-03")


def test_remote_text_is_flattened_before_it_reaches_the_issue(data_root):
    _run(data_root, "2026-10-02T121700Z", status="partial_success",
         flags=["arxiv_failed\n| drop table |`" + "x" * 300])
    alert = alerts.render(data_root, job="run", conclusion="success", run_url="", now=NOW)
    assert "\n| drop" not in alert.body and "`" not in alert.body.split("## 当前警告")[1]
    assert alerts.plain("a\r\nb|c`d", 4) == "a b…"


def test_write_files_and_github_output(data_root, tmp_path):
    _run(data_root, "2026-10-02T121700Z")
    out = tmp_path / "alert"
    gh = tmp_path / "out.txt"
    alerts.write_files(alerts.render(data_root, job="run", conclusion="failure", run_url="u", now=NOW), out, gh)
    assert (out / "alert-body.md").exists() and (out / "alert-comment.md").exists()
    assert "alert=true" in gh.read_text(encoding="utf-8")
    alerts.write_files(alerts.render(data_root, job="run", conclusion="success", run_url="u", now=NOW), out)
    assert not (out / "alert-comment.md").exists()  # a stale comment file never survives
