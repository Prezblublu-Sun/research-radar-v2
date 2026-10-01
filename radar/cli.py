"""``radar`` command line. Subcommands are added as their phase lands."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from radar import config as _config
from radar.paths import DataRoot
from radar.pipeline import prompt as _prompt
from radar.pipeline.context import Fetchers, RunContext

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_PROMPT = "scorer_v4.txt"


def _repo(args) -> pathlib.Path:
    return pathlib.Path(args.repo).resolve() if getattr(args, "repo", None) else REPO


def _prompt_file(args) -> str:
    return getattr(args, "prompt", None) or os.environ.get("SCORER_PROMPT_FILE") or DEFAULT_PROMPT


# ---------------------------------------------------------------------------
# radar daily
# ---------------------------------------------------------------------------

def cmd_daily(args) -> int:
    from radar.pipeline import daily as _daily
    from radar.pipeline.scorer import DeepSeekScorer, DryRunScorer
    from radar.store.seen import SeenKeys

    repo = _repo(args)
    cfg = _config.load(repo / "config" / "directions.yaml")
    bundle = _prompt.load(repo / "prompts", _prompt_file(args))
    data_root = DataRoot.at(repo / "data")

    if args.dry_run:
        out_root = DataRoot.at(pathlib.Path(args.out or (repo / ".radar-dryrun")) / "data")
        scorer = DryRunScorer()
    else:
        out_root = data_root
        scorer = DeepSeekScorer(prompt=bundle, failures_dir=data_root.cache / "llm_failures")

    fetchers = _fixture_fetchers(pathlib.Path(args.fixture)) if args.fixture else Fetchers.real()

    ctx = RunContext(
        data_root=out_root, config=cfg, prompt=bundle, scorer=scorer, fetchers=fetchers,
        today=dt.date.fromisoformat(args.date) if args.date else dt.date.today(),
        days_back=args.days, random_reading=not args.no_random, force=args.force,
        dry_run=args.dry_run, offline=args.offline,
    )
    # Dry runs still dedup against the real corpus so the numbers are honest.
    seen = SeenKeys.load(data_root)
    report = _daily.run_daily(ctx, seen=seen)
    if args.dry_run and report.papers:
        _print_routing_table(report.papers, cfg)
    print(json.dumps({"run_id": ctx.run_id, "run_status": report.header["run_status"],
                      "quality_flags": report.header["quality_flags"],
                      "counts": report.header["counts"],
                      "random_reading": report.header["random_reading"],
                      "run_path": str(report.run_path) if report.run_path else None,
                      "random_path": str(report.random_path) if report.random_path else None},
                     ensure_ascii=False, indent=1))
    return 0 if report.ok else 1


def _fixture_fetchers(path: pathlib.Path) -> Fetchers:
    """A JSONL of normalised records stands in for the network."""
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    class Stub:
        def __init__(self, source): self.source = source
        def fetch(self, *args, **kwargs):
            return [dict(r) for r in records if r.get("source") == self.source]

    return Fetchers(arxiv=Stub("arxiv"), openalex=Stub("openalex"), pubmed=Stub("pubmed"))


def _print_routing_table(papers: list[dict], cfg) -> None:
    from collections import Counter
    print("\nrouting (dry run):")
    for key in cfg.keys:
        mine = [p for p in papers if p.get("direction") == key]
        terms = Counter(t for p in mine for t in (p.get("routing_matches") or {}).get(key, []))
        top = ", ".join(f"{t} ×{n}" for t, n in terms.most_common(4))
        print(f"  {key:22s} {len(mine):4d}   {top}")


# ---------------------------------------------------------------------------
# radar health / doctor / alerts
# ---------------------------------------------------------------------------

def cmd_health(args) -> int:
    """Verdict on one run header (newest by default); non-zero only when blocking."""
    from radar.pipeline import health as _health
    from radar.store import runs as _runs

    root = DataRoot.at(_repo(args) / "data")
    files = _runs.list_runs(root)
    if args.run_id:
        files = [p for p in files if p.name.startswith(args.run_id)]
    if not files:
        print("::error::no run log found" + (f" for {args.run_id}" if args.run_id else ""))
        return 1
    path = files[-1]
    try:
        header = _runs.read_header(path)
    except Exception as error:  # noqa: BLE001
        print(f"::error::{path.name} is not a readable run log: {error}")
        return 1
    verdict = _health.evaluate(header)
    if args.max_age_hours is not None:
        stamp = header.get("finished_at") or ""
        try:
            finished = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - finished).total_seconds() / 3600
            if age > args.max_age_hours:
                verdict.blocking.append(f"newest run {path.name} finished {age:.1f} h ago (> {args.max_age_hours} h)")
        except ValueError:
            verdict.blocking.append(f"{path.name}: unreadable finished_at {stamp!r}")
    print(f"health: {path.name} run_status={header.get('run_status')} flags={header.get('quality_flags')}")
    for line in verdict.warnings:
        print(f"::warning::{line}")
    for line in verdict.blocking:
        print(f"::error::{line}")
    print("health: " + ("OK" if verdict.ok else "BLOCKED"))
    return 0 if verdict.ok else 1


def cmd_doctor(args) -> int:
    from radar.ops import doctor
    repo = _repo(args)
    report = doctor.check(repo, DataRoot.at(repo / "data"), _prompt_file(args))
    print(doctor.render(report))
    return 0 if report.ok else 1


def cmd_alerts_render(args) -> int:
    from radar.ops import alerts
    root = DataRoot.at(_repo(args) / "data")
    alert = alerts.render(root, job=args.job, conclusion=args.conclusion, run_url=args.run_url or "",
                          max_age_hours=args.max_age_hours)
    alerts.write_files(alert, pathlib.Path(args.out_dir),
                       pathlib.Path(args.github_output) if args.github_output else None)
    print(alert.body)
    print(f"alerts: {'COMMENT' if alert.alerting else 'body only'} -> {args.out_dir}")
    return 0


# ---------------------------------------------------------------------------
# radar eval
# ---------------------------------------------------------------------------

def cmd_eval_route(args) -> int:
    from radar.eval import picks as _picks
    repo = _repo(args)
    cfg = _config.load(repo / "config" / "directions.yaml")
    fixture = _picks.load_fixture(pathlib.Path(args.fixture or (repo / "tests" / "fixtures" / "eval" / "picks.jsonl")))
    report = _picks.route_report(fixture, cfg)
    print(f"picks: {report['n']}  routed: {report['routed']}  recall: {report['recall']:.1%}  "
          f"primary agrees with fixture: {report['agreement']:.1%}")
    keys = cfg.keys + ["UNROUTED"]
    print("\nexpected \\ primary " + " ".join(f"{k[:10]:>10s}" for k in keys))
    for expected in keys:
        row = report["table"].get(expected, {})
        if not row:
            continue
        print(f"{expected[:18]:18s} " + " ".join(f"{row.get(k, 0):10d}" for k in keys))
    for title in report["misses"]:
        print("  UNROUTED:", title[:90])
    return 0 if report["recall"] >= 0.95 else 1


def cmd_eval_build(args) -> int:
    """Build tests/fixtures/eval/picks.jsonl from the pasted list + corpus records."""
    from radar.eval import picks as _picks
    repo = _repo(args)
    cfg = _config.load(repo / "config" / "directions.yaml")
    text = pathlib.Path(args.picks).read_text(encoding="utf-8")
    records = [json.loads(line) for line in pathlib.Path(args.records).read_text(encoding="utf-8").splitlines() if line.strip()]
    picks = _picks.parse_picks_md(text)
    resolved, missing = _picks.resolve_picks(picks, records, cfg)
    _picks.write_fixture(pathlib.Path(args.out), resolved)
    print(f"picks parsed: {len(picks)}  resolved: {len(resolved)}  missing: {len(missing)} -> {args.out}")
    for pick in missing:
        print("  missing:", pick.identity_key or pick.title[:80])
    return 0


# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar", description=__doc__)
    parser.add_argument("--repo", help="repository root (default: this checkout)")
    parser.add_argument("--prompt", help="scorer prompt file name (default: $SCORER_PROMPT_FILE or scorer_v4.txt)")
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="fetch → dedup → route → score → random reading → persist")
    daily.add_argument("--days", type=int, default=2, help="requested lookback (per-source floors apply)")
    daily.add_argument("--date", help="treat this YYYY-MM-DD as today")
    daily.add_argument("--dry-run", action="store_true", help="no scoring, write under .radar-dryrun/")
    daily.add_argument("--fixture", help="JSONL of normalised records instead of the network")
    daily.add_argument("--offline", action="store_true", help="no network at all (cached aliases, no random draw)")
    daily.add_argument("--out", help="dry-run output directory")
    daily.add_argument("--force", action="store_true", help="bypass dedup (never writes the seen cache)")
    daily.add_argument("--no-random", action="store_true", help="skip the random-reading stage")
    daily.set_defaults(func=cmd_daily)

    health = sub.add_parser("health", help="verdict on the newest run log (the workflow health gate)")
    health.add_argument("--run-id", help="a specific run id instead of the newest")
    health.add_argument("--max-age-hours", type=float, help="block if the newest run is older than this")
    health.set_defaults(func=cmd_health)

    doctor = sub.add_parser("doctor", help="offline health check of this checkout")
    doctor.set_defaults(func=cmd_doctor)

    alerts = sub.add_parser("alerts", help="standing-issue alerts")
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    render = alerts_sub.add_parser("render", help="write alert-body.md and, on failure, alert-comment.md")
    render.add_argument("--job", required=True)
    render.add_argument("--conclusion", required=True, help="success | failure | cancelled | skipped")
    render.add_argument("--run-url", default="")
    render.add_argument("--out-dir", required=True)
    render.add_argument("--github-output", help="append alert=true|false etc. to this file")
    render.add_argument("--max-age-hours", type=float, default=30.0)
    render.set_defaults(func=cmd_alerts_render)

    ev = sub.add_parser("eval", help="ground-truth checks")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    route = ev_sub.add_parser("route", help="router recall over the picks fixture")
    route.add_argument("--fixture")
    route.set_defaults(func=cmd_eval_route)
    build = ev_sub.add_parser("build-fixture", help="resolve a pasted picks list against corpus records")
    build.add_argument("--picks", required=True)
    build.add_argument("--records", required=True, help="JSONL of corpus records (title, abstract, doi/arxiv_id, …)")
    build.add_argument("--out", required=True)
    build.set_defaults(func=cmd_eval_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
