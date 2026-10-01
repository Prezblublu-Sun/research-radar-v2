# ADR-0001: Append-only run logs, derived state, rendered prompts

Status: accepted (2026-10-01). Supersedes the v1 data layout
(`Prezblublu-Sun/research-radar`, ADR-0015/0017/0030/0031 there).

## Context

v1 ran for five months and accumulated:

- a 5.15 GB repository, because every daily run rewrote large files
  (`candidates.jsonl` 85 MB, the generated site 1.4 GB, per-date buckets
  merged in place, a 3.3 MB visuals index);
- a dedup state file (`seen_dois.json`) written non-atomically, whose
  truncation would have silently reset dedup for the whole fetch window;
- a 436-line orchestrator that could only be tested by monkeypatching;
- a scorer that sent the literal template placeholder `{direction_context}`
  to the model, because nothing ever rendered the system prompt.

The reader's new search framework (six keyword groups derived from the
121 papers they actually chose to read) made a fresh repository the cheaper
path.

## Decisions

1. **One immutable file per run.** `data/runs/YYYY/<run_id>-<type>.jsonl`,
   `run_id = YYYY-MM-DDTHHMMSSZ`. Line 1 is the run header (what v1 called
   the manifest: hashes of config and prompt, source status, token usage,
   counts, quality flags). Every later line is one paper in the v1 key set,
   kept flat, plus `ck` (canonical key) as the **first** key, `run_id`,
   `scored_at`, `scorer_version`, `prompt_sha`, `crossover`, `provenance`.
   `write_jsonl_new` refuses to overwrite. A correction is a new run of
   type `rescore`; at read time the newest `scored_at` whose scorer did not
   fail wins (`store/corpus.py`, Phase 3).
2. **Seen keys are derived.** `store/seen.py` rebuilds the set by scanning
   the `{"ck":"…"` prefix of every line under `data/runs/` (regex, no JSON
   parse). A cache under the gitignored `.radar-cache/` is used only when it
   describes the current run directory exactly; a damaged cache costs a
   rebuild, never a dedup reset.
3. **Empty run writes nothing.** `fetched_total == 0` sets
   `run_status: failed` + `fetched_zero`, writes no file, and the health
   verdict blocks publishing.
4. **Stages are pure functions on a frozen `RunContext`.** Fetch
   (fail-soft per source), dedup, route, score, header, persist (must
   raise), health. No module reads the environment or the file system at
   import; every path comes from a `DataRoot`.
5. **The prompt is rendered.** `prompts/scorer_v4.txt` keeps the v3 output
   schema byte for byte; `{direction_context}` is filled with
   `str.replace` (the schema has braces), the never-filled metadata block is
   gone, and the user message carries the routed direction's focus, the
   crossover note and the paper. Prompt files are never edited; their
   hashes are pinned in a contract test.
6. **Routing is configuration.** Keyword rules, exclusions and the
   crossover-boost pairs all live in `config/directions.yaml`; the router
   tolerates a trailing plural `s`, which four of the 121 picks needed.
7. **Recall is measured.** `tests/fixtures/eval/picks.jsonl` freezes the
   reader's 121 picks with abstracts; `radar eval route` must route at
   least 95% of them and CI fails otherwise.

## Consequences

- Growth is bounded by what is new: ~0.8 MB/day working tree at 160
  papers/day, ~60 MB/year packed, nothing rewritten.
- The site, the seen set and every index are rebuilt from `data/`; a wrong
  derived file is a builder bug, never a data repair.
- `pip install -e ".[dev]"` then `pytest` is the whole onboarding and must
  stay green with no node, no plotting libraries and no secrets.
- Small snapshot files that the browser writes with a single `PUT`
  (`data/marks/`, `data/digest/`, `data/aliases/`) stay rewritten in
  place; none of them caused the bloat and an append-only layout would
  break autosync.
