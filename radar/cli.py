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

    if args.fixture:
        fetchers = _fixture_fetchers(pathlib.Path(args.fixture))
    else:
        fetchers = Fetchers.real()

    ctx = RunContext(
        data_root=out_root, config=cfg, prompt=bundle, scorer=scorer, fetchers=fetchers,
        today=dt.date.fromisoformat(args.date) if args.date else dt.date.today(),
        days_back=args.days, random_reading=not args.no_random, force=args.force,
        dry_run=args.dry_run, offline=args.offline,
    )
    # Dry runs still dedup against the real corpus so the numbers are honest.
    from radar.store.seen import SeenKeys
    seen = SeenKeys.load(data_root)
    report = _daily.run_daily(ctx, seen=seen)
    if args.dry_run and report.papers:
        _print_routing_table(report.papers, cfg)
    print(json.dumps({"run_id": ctx.run_id, "run_status": report.header["run_status"],
                      "quality_flags": report.header["quality_flags"],
                      "counts": report.header["counts"],
                      "run_path": str(report.run_path) if report.run_path else None},
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
# radar doctor / eval
# ---------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    from radar.ops import doctor
    repo = _repo(args)
    report = doctor.check(repo, DataRoot.at(repo / "data"), _prompt_file(args))
    print(doctor.render(report))
    return 0 if report.ok else 1


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

    daily = sub.add_parser("daily", help="fetch → dedup → route → score → persist")
    daily.add_argument("--days", type=int, default=2, help="requested lookback (per-source floors apply)")
    daily.add_argument("--date", help="treat this YYYY-MM-DD as today")
    daily.add_argument("--dry-run", action="store_true", help="no scoring, write under .radar-dryrun/")
    daily.add_argument("--fixture", help="JSONL of normalised records instead of the network")
    daily.add_argument("--offline", action="store_true", help="no network at all (implies cached aliases only)")
    daily.add_argument("--out", help="dry-run output directory")
    daily.add_argument("--force", action="store_true", help="bypass dedup (never writes the seen cache)")
    daily.add_argument("--no-random", action="store_true", help="skip the random-reading stage")
    daily.set_defaults(func=cmd_daily)

    doctor = sub.add_parser("doctor", help="offline health check of this checkout")
    doctor.set_defaults(func=cmd_doctor)

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
