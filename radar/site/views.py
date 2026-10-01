"""JSON shards and the data each page shell needs.

Every function here writes files under the output directory or returns
plain dicts for a template; none of them render HTML. Shapes consumed by the
ported bundles (day shards, queue, search) are the v1 shapes unchanged.
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from radar.core.records import utc_now_iso
from radar.store.corpus import PRIORITIES, PRIORITY_ORDER, Corpus, display_priority, priority_counts
from radar.site import public

DAY_PAGE_SIZE = 20
WORKBENCH_RUNS = 7
WORKBENCH_RUN_CARD_CAP = 150
RANDOM_RUNS = 30
STATUS_RUNS = 14
MEGAJOURNAL_WORKS = 200   # shown, not applied; mirrors pipeline.random_reading


def write_json(path: pathlib.Path, payload) -> None:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# day shards (v1 shape; radar-day.js and radar-reading.js read them)
# ---------------------------------------------------------------------------

def day_shards(out: pathlib.Path, bucket_date: str, papers: list[dict], config, *,
               date_precision: str = "day", previous_date: str | None = None,
               next_date: str | None = None, visuals: dict | None = None) -> dict:
    keys = public.public_keys_for_day(papers, bucket_date)
    positioned = sorted(enumerate(papers), key=lambda item: (
        PRIORITY_ORDER.get(display_priority(item[1]), 9), item[1].get("direction") or "zzz", item[0]))
    records = []
    for position, paper in positioned:
        ident, anchor = keys[id(paper)]
        records.append(public.public_card_record(bucket_date, paper, position, config,
                                                 identity_key=ident, anchor=anchor,
                                                 visual=(visuals or {}).get(ident)))
    counts = {p: sum(1 for paper in papers if display_priority(paper) == p)
              for p in ("High", "Medium", "Unscored", "Low", "Exclude")}
    page_count = (len(records) + DAY_PAGE_SIZE - 1) // DAY_PAGE_SIZE
    revision = hashlib.sha256(json.dumps({"date": bucket_date, "records": records}, ensure_ascii=False,
                                         sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    day_dir = out / "data" / "day" / bucket_date
    for index in range(page_count):
        write_json(day_dir / f"page-{index + 1}.json", {
            "schema_version": 1, "date": bucket_date, "page": index + 1, "page_count": page_count,
            "revision": revision, "papers": records[index * DAY_PAGE_SIZE:(index + 1) * DAY_PAGE_SIZE]})
    manifest = {
        "schema_version": 1, "date": bucket_date, "date_precision": date_precision or "day",
        "total": len(records), "page_size": DAY_PAGE_SIZE, "page_count": page_count,
        "priority_counts": counts, "previous_date": previous_date, "next_date": next_date,
        "revision": revision, "page_pattern": "page-{page}.json",
        "anchor_pages": {r["anchor"]: i // DAY_PAGE_SIZE + 1 for i, r in enumerate(records)},
    }
    write_json(day_dir / "manifest.json", manifest)   # the commit point
    return manifest


# ---------------------------------------------------------------------------
# queue (v1 shape; radar-queue.js)
# ---------------------------------------------------------------------------

def queue_shards(out: pathlib.Path, corpus: Corpus, config, visuals: dict | None = None) -> dict:
    grouped: dict[str, dict[str, list[dict]]] = {"High": {}, "Medium": {}}
    for bucket_date, papers in corpus.buckets.items():
        keys = public.public_keys_for_day(papers, bucket_date)
        for position, paper in enumerate(papers):
            priority = (paper.get("llm") or {}).get("priority")
            if priority not in grouped:
                continue
            ident, anchor = keys[id(paper)]
            grouped[priority].setdefault(bucket_date[:4], []).append(public.public_card_record(
                bucket_date, paper, position, config, identity_key=ident, anchor=anchor,
                visual=(visuals or {}).get(ident)))
    priorities = {}
    for priority, years in grouped.items():
        year_counts, year_facets = {}, {}
        for year, records in years.items():
            records.sort(key=lambda r: (r["date"], r["title"]), reverse=True)
            write_json(out / f"queue-{priority.lower()}-{year}.json", records)
            year_counts[year] = len(records)
            directions: dict[str, int] = {}
            relevance: dict[str, int] = {}
            pairs: dict[str, dict[str, int]] = {}
            for r in records:
                d, rel = r.get("direction") or "", r.get("relevance_level") or ""
                if d:
                    directions[d] = directions.get(d, 0) + 1
                if rel:
                    relevance[rel] = relevance.get(rel, 0) + 1
                if d and rel:
                    pairs.setdefault(d, {})[rel] = pairs.setdefault(d, {}).get(rel, 0) + 1
            year_facets[year] = {"total": len(records), "directions": dict(sorted(directions.items())),
                                 "relevance": dict(sorted(relevance.items())),
                                 "direction_relevance": {d: dict(sorted(c.items())) for d, c in sorted(pairs.items())}}
        priorities[priority] = {"total": sum(year_counts.values()),
                                "years": {y: year_counts[y] for y in sorted(year_counts, reverse=True)},
                                "year_facets": {y: year_facets[y] for y in sorted(year_facets, reverse=True)}}
    manifest = {"schema_version": 2, "generated_at": corpus.generated_at, **corpus.stats.as_dict(),
                "priorities": priorities,
                "directions": {key: {"name": config.display_name(key), "color": config.color(key)} for key in config.keys}}
    write_json(out / "queue-manifest.json", manifest)
    return manifest


# ---------------------------------------------------------------------------
# search (v1 shape; radar-search.js + worker)
# ---------------------------------------------------------------------------

def search_index(out: pathlib.Path, corpus: Corpus, config) -> int:
    by_year: dict[str, list] = {}
    deep_by_year: dict[str, list] = {}
    for bucket_date, papers in sorted(corpus.buckets.items()):
        keys = public.public_keys_for_day(papers, bucket_date)
        for paper in papers:
            llm = paper.get("llm") or {}
            terms = []
            for term in llm.get("key_terms") or []:
                text = (" · ".join(str(term.get(k) or "").strip() for k in ("en", "zh") if term.get(k))
                        if isinstance(term, dict) else str(term).strip())
                if text:
                    terms.append(text)
            authors = [str(a) for a in (paper.get("authors") or [])]
            ident, anchor = keys[id(paper)]
            year = bucket_date[:4]
            by_year.setdefault(year, []).append({
                "identity_key": ident, "anchor": anchor, "date": bucket_date,
                "title": (paper.get("title") or "")[:200],
                "authors": ", ".join(authors[:3]) + (" et al." if len(authors) > 3 else ""),
                "venue": (paper.get("venue") or "")[:80],
                "direction": paper.get("direction") or "",
                "direction_name": paper.get("direction_name") or "",
                "priority": llm.get("priority") or "",
                "relevance_level": llm.get("relevance_level") or "",
                "tags": (llm.get("tags") or [])[:2],
                "term": terms[0] if terms else "",
            })
            deep = [public.flatten_text(paper.get("abstract") or "")[:500],
                    public.flatten_text(llm.get("relevance_to_user")), public.flatten_text(llm.get("why_not_core")),
                    public.flatten_text(llm.get("summary_zh")), " ".join(terms)]
            deep_by_year.setdefault(year, []).append({"identity_key": ident,
                                                      "deep_blob": " ".join(p for p in deep if p).lower()})
    years = sorted(by_year, reverse=True)
    for year in years:
        write_json(out / f"search-index-{year}.json", by_year[year])
        write_json(out / f"search-deep-{year}.json", deep_by_year[year])
    total = sum(len(v) for v in by_year.values())
    write_json(out / "search-index-manifest.json", {
        "schema_version": 2, "years": years, "counts": {y: len(by_year[y]) for y in years},
        "deep_counts": {y: len(deep_by_year[y]) for y in years}, "total": total,
        "meta_pattern": "search-index-{year}.json", "deep_pattern": "search-deep-{year}.json",
        "generated_at": corpus.generated_at, **corpus.stats.as_dict()})
    return total


# ---------------------------------------------------------------------------
# workbench run shards (v2; radar-workbench.js)
# ---------------------------------------------------------------------------

def run_shards(out: pathlib.Path, corpus: Corpus, config, *, limit: int = WORKBENCH_RUNS,
               cap: int = WORKBENCH_RUN_CARD_CAP, visuals: dict | None = None) -> list[dict]:
    """The last ``limit`` daily runs with their papers, newest first."""
    by_run = corpus.by_first_run
    summaries = []
    for header in corpus.headers:
        if header.get("run_type") != "daily" or header.get("run_status") not in ("success", "partial_success"):
            continue
        run_id = header["run_id"]
        papers = sorted(by_run.get(run_id, []), key=lambda p: (
            PRIORITY_ORDER.get(display_priority(p), 9), str(p.get("date") or ""), str(p.get("title") or "")))
        embedded = papers[:cap]
        records = []
        for position, paper in enumerate(embedded):
            bucket = str(paper.get("date") or "")[:10] or str(paper.get("first_seen_at") or "")[:10]
            ident = public.public_identity_key(paper, bucket, position)
            records.append(public.public_card_record(bucket, paper, position, config, identity_key=ident,
                                                     visual=(visuals or {}).get(ident)))
        counts = priority_counts(papers)
        write_json(out / "data" / "run" / f"{run_id}.json", {
            "schema_version": 1, "run_id": run_id, "run_type": header.get("run_type"),
            "run_status": header.get("run_status"), "quality_flags": header.get("quality_flags") or [],
            "total": len(papers), "embedded": len(embedded), "priority_counts": counts, "records": records})
        summaries.append({
            "run_id": run_id, "date": run_id[:10], "run_type": header.get("run_type"),
            "run_status": header.get("run_status"), "quality_flags": header.get("quality_flags") or [],
            "total": len(papers), "embedded": len(embedded), "priority_counts": counts,
            "llm": header.get("llm") or {}, "random_reading": header.get("random_reading") or {},
            "pills": pills(counts),
        })
        if len(summaries) >= limit:
            break
    return summaries


def pills(counts: dict) -> list[tuple[str, str]]:
    """``[(css modifier, label)]`` for the non-zero priority counts."""
    out = []
    for key, suffix, mod in (("High", "H", "high"), ("Medium", "M", "medium"),
                             ("Low", "L", "low"), ("Exclude", "X", "exclude")):
        n = int(counts.get(key, 0) or 0)
        if n:
            out.append((mod, f"{n}{suffix}"))
    return out


# ---------------------------------------------------------------------------
# random reading (v2; radar-random.js)
# ---------------------------------------------------------------------------

def random_payloads(out: pathlib.Path, random_runs: list[tuple[dict, list[dict]]], config) -> list[dict]:
    days = []
    for header, records in random_runs[:RANDOM_RUNS]:
        run_id = header.get("run_id") or ""
        cards = []
        for position, paper in enumerate(records):
            bucket = str(paper.get("date") or "")[:10] or header.get("date", "")
            card = public.public_card_record(bucket, paper, position, config)
            card["random_reading"] = paper.get("random_reading") or {}
            cards.append(card)
        write_json(out / "data" / "random" / f"{run_id}.json", {
            "schema_version": 1, "run_id": run_id, "date": header.get("date", ""),
            "journals": header.get("journals") or [], "records": cards})
        journals = []
        for journal in header.get("journals") or []:
            pool = int(journal.get("month_works") or 0)
            meta = [f"{journal.get('month', '')} 共 {pool} 篇"]
            if journal.get("target_picks"):
                meta.append(f"{'大刊' if pool > MEGAJOURNAL_WORKS else '专业刊'}，抽 {journal['target_picks']} 篇")
            if journal.get("truncated_pool"):
                meta.append("抽样仅覆盖前 10,000 篇")
            if journal.get("skipped_known"):
                skipped = min(int(journal["skipped_known"]), pool) if pool else journal["skipped_known"]
                meta.append(f"跳过 {skipped} 篇已在库")
            if journal.get("error"):
                meta.append(f"抽取失败：{journal['error']}")
            journals.append({"venue_id": journal.get("venue_id", ""), "venue": journal.get("venue", ""),
                             "seed_title": journal.get("seed_title", ""), "meta": " · ".join(meta),
                             "papers": sum(1 for c in cards if c["random_reading"].get("venue_id") == journal.get("venue_id"))})
        days.append({"run_id": run_id, "date": header.get("date", ""), "counts": header.get("counts") or {},
                     "top_up_of": header.get("top_up_of") or "", "journals": journals})
    return days


# ---------------------------------------------------------------------------
# month / archive / status / manifest
# ---------------------------------------------------------------------------

def _trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def day_top_paper(papers: list[dict], config) -> dict | None:
    top = next((p for want in ("High", "Medium") for p in papers
                if (p.get("llm") or {}).get("priority") == want), None)
    if top is None:
        return None
    llm = top.get("llm") or {}
    zh, en = llm.get("summary_zh") or {}, llm.get("summary_en") or {}

    def field_(name):
        v = zh.get(name) or en.get(name) or ""
        return v.strip() if isinstance(v, str) else ""

    direction = top.get("direction") or ""
    return {"title": _trunc(top.get("title") or "", 90), "is_high": llm.get("priority") == "High",
            "authors": [str(a) for a in (top.get("authors") or []) if str(a).strip()],
            "direction": direction, "direction_name": top.get("direction_name") or "",
            "direction_color": config.color(direction) if direction else "#888",
            "venue": (top.get("venue") or "").strip() or (top.get("source") or "").strip(),
            "tags": [str(t).strip() for t in (llm.get("tags") or []) if str(t).strip()][:3],
            "motivation": _trunc(field_("motivation"), 80), "method": field_("method"),
            "result": field_("result"), "validation": field_("validation"),
            "relevance_to_user": (llm.get("relevance_to_user") or "").strip()}


def month_views(corpus: Corpus, config) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    """``months -> [day view]`` and the per-month list of day counts."""
    months: dict[str, list[dict]] = {}
    month_counts: dict[str, list[dict]] = {}
    for date in sorted(corpus.buckets, reverse=True):
        papers = corpus.buckets[date]
        counts = priority_counts(papers)
        month = date[:7]
        months.setdefault(month, []).append({
            "date": date, "counts": counts, "pills": pills(counts),
            "low_quality": counts.get("High", 0) + counts.get("Medium", 0) == 0,
            "top": day_top_paper(papers, config)})
        month_counts.setdefault(month, []).append(counts)
    return months, month_counts


def archive_view(months: dict[str, list[dict]], month_counts: dict[str, list[dict]], current_month: str) -> dict:
    def month_card(mk):
        agg: dict[str, int] = {}
        for c in month_counts.get(mk, []):
            for k in PRIORITIES:
                if c.get(k):
                    agg[k] = agg.get(k, 0) + c[k]
        return {"month": mk, "pills": pills(agg), "low_quality": agg.get("High", 0) + agg.get("Medium", 0) == 0,
                "days": len(months.get(mk, []))}
    ordered = sorted(months, reverse=True)
    future = [m for m in ordered if current_month and m > current_month]
    regular = [m for m in ordered if m not in set(future)]
    by_year: dict[str, list[dict]] = {}
    for month in regular:
        by_year.setdefault(month[:4], []).append(month_card(month))
    active_year = (current_month or (regular[0] if regular else ""))[:4]
    return {"years": [{"year": y, "open": y == active_year, "months": by_year[y]} for y in sorted(by_year, reverse=True)],
            "future": [month_card(m) for m in future]}


def status_rows(headers: list[dict], limit: int = STATUS_RUNS) -> list[dict]:
    rows = []
    for h in headers[:limit]:
        counts = h.get("counts") or {}
        prio = counts.get("priority_counts") or {}
        rows.append({"run_id": h.get("run_id", ""), "run_type": h.get("run_type", ""),
                     "run_status": h.get("run_status", "—"), "quality_flags": h.get("quality_flags") or [],
                     "fetched": counts.get("fetched", "—"), "after_dedup": counts.get("after_dedup", "—"),
                     "after_routing": counts.get("after_routing", "—"),
                     "high_medium": f"{prio.get('High', '—')}/{prio.get('Medium', '—')}",
                     "usd": (h.get("llm") or {}).get("estimated_usd", "—"),
                     "random": (h.get("random_reading") or {}).get("papers", "—"),
                     "commit": (h.get("git_commit") or "")[:7]})
    return rows


def site_manifest(corpus: Corpus, config, repo: str, assets: dict[str, str]) -> dict:
    latest = corpus.headers[0] if corpus.headers else {}
    return {"schema_version": 1, "generated_at": utc_now_iso(), "repo": repo,
            "corpus": corpus.stats.as_dict(), "directions": config.keys,
            "latest_run": {k: latest.get(k) for k in ("run_id", "run_type", "run_status", "quality_flags", "finished_at")},
            "assets": assets}
