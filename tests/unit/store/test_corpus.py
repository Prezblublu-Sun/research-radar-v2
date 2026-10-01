"""The corpus merge rule: earliest identity, newest valid verdict, no fuzz."""
from __future__ import annotations

from radar.core.atomic import write_jsonl_new
from radar.store import corpus


def _rec(ck, run_id, *, first_seen, scored_at, priority="Medium", failed=False, title="t", **extra):
    llm = {"priority": priority}
    if failed:
        llm = {"priority": None, "scorer_failed": True}
    rec = {"ck": ck, "kind": "paper", "run_id": run_id, "identity_key": ck.replace("doi:", "doi:") if ck else "",
           "first_seen_at": first_seen, "scored_at": scored_at, "title": title, "date": "2026-09-01",
           "llm": llm, "direction": "stem_biomech"}
    rec.update(extra)
    return rec


def _write(root, run_id, records, run_type="daily", status="success"):
    write_jsonl_new(root.run_file(run_id, run_type),
                    [{"kind": "run", "run_id": run_id, "run_type": run_type, "run_status": status}] + records)


def test_earliest_identity_and_newest_valid_verdict_win(data_root):
    _write(data_root, "2026-09-01T120000Z", [
        _rec("doi:10.1/a", "2026-09-01T120000Z", first_seen="2026-09-01T12:00:00Z", scored_at="2026-09-01T12:00:01Z",
             priority="Low", identity_key="doi:10.1/A", title="first")])
    _write(data_root, "2026-09-05T120000Z", [
        _rec("doi:10.1/a", "2026-09-05T120000Z", first_seen="2026-09-05T12:00:00Z", scored_at="2026-09-05T12:00:01Z",
             failed=True, identity_key="doi:10.1/a", title="failed rescore")], run_type="rescore")
    _write(data_root, "2026-09-03T120000Z", [
        _rec("doi:10.1/a", "2026-09-03T120000Z", first_seen="2026-09-03T12:00:00Z", scored_at="2026-09-03T12:00:01Z",
             priority="High", identity_key="doi:10.1/a", title="rescored")], run_type="rescore")
    c = corpus.load_corpus(data_root)
    assert c.stats.raw_total == 3 and c.stats.unique_total == 1 and c.stats.duplicates_suppressed == 2
    [paper] = c.papers
    assert paper["identity_key"] == "doi:10.1/A"            # earliest record names the anchor
    assert paper["first_seen_at"] == "2026-09-01T12:00:00Z"
    assert paper["first_run_id"] == "2026-09-01T120000Z"
    assert paper["llm"]["priority"] == "High" and paper["title"] == "rescored"   # newest that did not fail
    assert paper["runs"] == ["2026-09-01T120000Z", "2026-09-03T120000Z", "2026-09-05T120000Z"]
    assert [h["run_id"] for h in c.headers][0] == "2026-09-05T120000Z"


def test_all_failed_keeps_the_newest_failure_as_unscored(data_root):
    _write(data_root, "2026-09-01T120000Z", [
        _rec("doi:10.1/b", "2026-09-01T120000Z", first_seen="2026-09-01T12:00:00Z", scored_at="2026-09-01T12:00:01Z", failed=True)])
    c = corpus.load_corpus(data_root)
    assert corpus.display_priority(c.papers[0]) == "Unscored"
    assert c.stats.priority_counts["Unscored"] == 1


def test_identity_less_records_are_never_merged(data_root):
    _write(data_root, "2026-09-01T120000Z", [
        _rec("", "2026-09-01T120000Z", first_seen="2026-09-01T12:00:00Z", scored_at="2026-09-01T12:00:01Z", title="Same"),
        _rec("", "2026-09-01T120000Z", first_seen="2026-09-01T12:00:00Z", scored_at="2026-09-01T12:00:02Z", title="Same")])
    c = corpus.load_corpus(data_root)
    assert c.stats.unique_total == 2 and c.stats.duplicates_suppressed == 0


def test_buckets_by_publication_date_with_fallback(data_root):
    _write(data_root, "2026-09-01T120000Z", [
        _rec("doi:10.1/c", "2026-09-01T120000Z", first_seen="2026-09-02T12:00:00Z", scored_at="2026-09-02T12:00:01Z", date=""),
        _rec("doi:10.1/d", "2026-09-01T120000Z", first_seen="2026-09-02T12:00:00Z", scored_at="2026-09-02T12:00:01Z", date="2026-08-30")])
    c = corpus.load_corpus(data_root)
    assert sorted(c.buckets) == ["2026-08-30", "2026-09-02"]
    assert c.by_first_run["2026-09-01T120000Z"] and c.generated_at == "2026-09-02T12:00:01Z"


def test_random_runs_are_a_separate_stream(data_root):
    _write(data_root, "2026-09-01T120000Z", [_rec("doi:10.1/e", "2026-09-01T120000Z", first_seen="x", scored_at="y")])
    path = data_root.random_reading / "2026" / "2026-09-01T120000Z.jsonl"
    write_jsonl_new(path, [{"kind": "random_reading", "run_id": "2026-09-01T120000Z", "date": "2026-09-01", "journals": []},
                           {"ck": "doi:10.9/r", "kind": "paper", "title": "random"}])
    assert corpus.load_corpus(data_root).stats.unique_total == 1       # never in the corpus
    [(header, records)] = corpus.load_random_runs(data_root)
    assert header["kind"] == "random_reading" and records[0]["title"] == "random"
