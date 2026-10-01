"""The command line entry points the workflows call."""
from __future__ import annotations

import datetime as dt
import json

from radar import cli
from radar.core.atomic import write_jsonl_new
from radar.core.records import utc_now_iso


def _repo_with_run(tmp_path, repo_root, *, status="success", flags=(), finished=None):
    """A fake repo root: real config/prompts, one run log under data/."""
    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "config" / "directions.yaml").write_bytes((repo_root / "config" / "directions.yaml").read_bytes())
    (repo / "prompts").mkdir()
    for p in (repo_root / "prompts").glob("*.txt"):
        (repo / "prompts" / p.name).write_bytes(p.read_bytes())
    header = {"kind": "run", "run_id": "2026-10-02T121700Z", "run_type": "daily", "run_status": status,
              "quality_flags": list(flags), "finished_at": finished or utc_now_iso(),
              "counts": {"scorer_failed": 0, "priority_counts": {"Medium": 3}}}
    write_jsonl_new(repo / "data" / "runs" / "2026" / "2026-10-02T121700Z-daily.jsonl",
                    [header, {"ck": "doi:a", "kind": "paper"}])
    return repo


def test_health_passes_a_fresh_success(tmp_path, repo_root, capsys):
    repo = _repo_with_run(tmp_path, repo_root)
    assert cli.main(["--repo", str(repo), "health", "--max-age-hours", "6"]) == 0
    assert "health: OK" in capsys.readouterr().out


def test_health_blocks_fetched_zero(tmp_path, repo_root, capsys):
    repo = _repo_with_run(tmp_path, repo_root, status="failed", flags=["fetched_zero"])
    assert cli.main(["--repo", str(repo), "health"]) == 1
    out = capsys.readouterr().out
    assert "::error::" in out and "fetched_zero" in out


def test_health_blocks_a_stale_run_only_when_asked(tmp_path, repo_root, capsys):
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
    repo = _repo_with_run(tmp_path, repo_root, finished=old)
    assert cli.main(["--repo", str(repo), "health"]) == 0
    assert cli.main(["--repo", str(repo), "health", "--max-age-hours", "6"]) == 1
    assert "h ago" in capsys.readouterr().out


def test_health_without_any_run_fails(tmp_path, repo_root, capsys):
    repo = _repo_with_run(tmp_path, repo_root)
    assert cli.main(["--repo", str(repo), "health", "--run-id", "1999-01-01T000000Z"]) == 1
    assert "no run log" in capsys.readouterr().out


def test_alerts_render_writes_files(tmp_path, repo_root, capsys):
    repo = _repo_with_run(tmp_path, repo_root)
    out = tmp_path / "alert"
    gh = tmp_path / "gh.txt"
    assert cli.main(["--repo", str(repo), "alerts", "render", "--job", "run", "--conclusion", "failure",
                     "--run-url", "https://x/9", "--out-dir", str(out), "--github-output", str(gh)]) == 0
    assert (out / "alert-comment.md").read_text(encoding="utf-8").count("https://x/9") == 1
    assert "alert=true" in gh.read_text(encoding="utf-8")
    assert "COMMENT" in capsys.readouterr().out


def test_daily_offline_fixture_dry_run_end_to_end(tmp_path, repo_root, capsys):
    repo = _repo_with_run(tmp_path, repo_root)
    fixture = repo_root / "tests" / "fixtures" / "fetch" / "sample.jsonl"
    code = cli.main(["--repo", str(repo), "daily", "--dry-run", "--offline", "--fixture", str(fixture),
                     "--out", str(tmp_path / "dry")])
    assert code == 0
    out = capsys.readouterr().out
    summary = json.loads(out[out.index("{\n"):])
    assert summary["run_status"] == "success" and summary["counts"]["after_routing"] > 0
    assert summary["random_reading"] == {"status": "skipped_offline"}
    assert summary["run_path"].startswith(str(tmp_path / "dry"))
    assert "routing (dry run):" in out
