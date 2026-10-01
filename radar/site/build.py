"""``radar site build``: data/ in, a static site out. Never commits anything.

Jinja renders shells, bars, navigation and status; every paper card is
rendered in the browser by ``radar-card.js`` from JSON shards. The same
validation runs locally and in CI (index exists, under 1 GB, no symlinks).
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field

from jinja2 import Environment, PackageLoader, select_autoescape

from radar.paths import DataRoot
from radar.site import assets as _assets
from radar.site import views as _views
from radar.store import corpus as _corpus

SITE_MAX_BYTES = 1_000_000_000
NAV = [("today", "index.html", "今日"), ("queue", "queue.html", "队列"), ("search", "search.html", "搜索"),
       ("library", "library.html", "资料库"), ("reading", "reading.html", "阅读清单"),
       ("random", "random-reading.html", "随机阅读"), ("archive", "archive.html", "归档")]


@dataclass
class BuildReport:
    out: pathlib.Path
    pages: list[str] = field(default_factory=list)
    unique_total: int = 0
    days: int = 0
    runs: int = 0
    bytes: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def environment() -> Environment:
    env = Environment(loader=PackageLoader("radar.site", "templates"),
                      autoescape=select_autoescape(default=True), trim_blocks=True, lstrip_blocks=True)
    env.globals["asset"] = _assets.asset
    env.globals["nav"] = NAV
    return env


def validate(out: pathlib.Path) -> list[str]:
    problems = []
    if not (out / "index.html").is_file():
        problems.append("index.html is missing")
    total = 0
    for path in out.rglob("*"):
        if path.is_symlink():
            problems.append(f"symlink in artifact: {path.relative_to(out)}")
        elif path.is_file():
            total += path.stat().st_size
    if total >= SITE_MAX_BYTES:
        problems.append(f"artifact is {total} bytes; must be under 1 GB")
    return problems


def build_site(data_root: DataRoot, out: pathlib.Path, config, *, repo: str = "",
               visuals: dict | None = None, log=print) -> BuildReport:
    out = pathlib.Path(out)
    out.mkdir(parents=True, exist_ok=True)
    env = environment()
    report = BuildReport(out=out)

    def render(template: str, name: str, **ctx) -> None:
        html = env.get_template(template).render(config=config, **ctx)
        (out / name).write_text(html, encoding="utf-8")
        report.pages.append(name)

    corpus = _corpus.load_corpus(data_root)
    report.unique_total = corpus.stats.unique_total
    log(f"corpus: {corpus.stats.unique_total} unique / {corpus.stats.raw_total} raw records, "
        f"{len(corpus.buckets)} publication dates, {len(corpus.headers)} runs")
    if visuals is None:
        from radar.store import visuals as _vstore
        from radar.visuals import public as _vpublic
        registry = _vstore.latest_visuals(data_root)
        visuals = _vpublic.safe_registry(registry)
        if registry:
            log(f"visuals: {len(visuals)} safe figure(s) of {len(registry)} registry records")

    # day pages + shards
    dates = sorted(corpus.buckets)
    for index, date in enumerate(dates):
        papers = corpus.buckets[date]
        manifest = _views.day_shards(out, date, papers, config,
                                     date_precision=next((p.get("date_precision") for p in papers if p.get("date_precision")), "day"),
                                     previous_date=dates[index - 1] if index > 0 else None,
                                     next_date=dates[index + 1] if index + 1 < len(dates) else None,
                                     visuals=visuals)
        render("day.html.j2", f"{date}.html", date=date, manifest=manifest,
               previous_date=manifest["previous_date"], next_date=manifest["next_date"],
               page_size=_views.DAY_PAGE_SIZE)
    report.days = len(dates)

    # month + archive
    months, month_counts = _views.month_views(corpus, config)
    current_month = corpus.headers[0]["run_id"][:7] if corpus.headers else ""
    archive_months = sorted(months)
    for index, month in enumerate(archive_months):
        render("month.html.j2", f"month-{month}.html", month=month, days=months[month],
               previous_month=archive_months[index - 1] if index > 0 else None,
               next_month=archive_months[index + 1] if index + 1 < len(archive_months) else None)
    render("archive.html.j2", "archive.html", archive=_views.archive_view(months, month_counts, current_month))

    # workbench
    runs = _views.run_shards(out, corpus, config, visuals=visuals)
    report.runs = len(runs)
    latest = corpus.headers[0] if corpus.headers else None
    render("index.html.j2", "index.html", runs=runs, latest=latest, stats=corpus.stats)

    # queue, search, reading, random, library, status
    _views.queue_shards(out, corpus, config, visuals=visuals)
    render("queue.html.j2", "queue.html")
    _views.search_index(out, corpus, config)
    render("search.html.j2", "search.html", total=corpus.stats.unique_total)
    render("reading.html.j2", "reading.html")
    random_days = _views.random_payloads(out, _corpus.load_random_runs(data_root), config)
    render("random.html.j2", "random-reading.html", days=random_days)
    render("library.html.j2", "library.html", repo=repo)
    render("status.html.j2", "status.html", rows=_views.status_rows(corpus.headers), latest=latest)
    for name, title, target in (("high-priority.html", "High-priority", "queue.html?priority=High"),
                                ("medium-priority.html", "Medium-priority", "queue.html?priority=Medium"),
                                ("my-marks.html", "My marks", "library.html#marks")):
        render("redirect.html.j2", name, title=title, target=target)

    copied = _assets.copy_static(out)
    _views.write_json(out / "site-manifest.json", _views.site_manifest(corpus, config, repo, _assets.versions()))
    (out / ".nojekyll").write_text("", encoding="utf-8")
    report.problems = validate(out)
    report.bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    log(f"site: {len(report.pages)} pages, {len(copied)} bundles, {report.bytes / 1e6:.1f} MB -> {out}")
    for problem in report.problems:
        log(f"::error::{problem}")
    return report
