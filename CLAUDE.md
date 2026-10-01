# Research Radar v2 — working agreement

Daily paper radar for one researcher (learning-based surrogates for
finite-element mechanics, applied to femoral stems and load-bearing
implants). Fetch → dedup → route → score → publish. This file is the
contract for anyone, human or model, changing it.

## 1. Optimisation target: recall, not precision

Radar's job is to **not miss valuable papers**. The LLM scorer is trusted to
grade noise correctly downstream.

- Prefer 80 candidates @ 40% relevance + accurate LLM grading over 19
  candidates @ 90% relevance.
- DeepSeek cost of ¥0.5–2/day is acceptable. Cost-saving is **not** a valid
  reason to cut recall.
- Do not propose tightening `must_pair_with`, adding restrictive routing
  rules, or broad exclusions "to reduce noise". Pairs widen reach; one
  strong keyword routes.
- "Adjacent-field inspiration" counts as value. Random reading (two to five
  papers drawn from each High paper's journal) exists to buy it directly;
  most draws score Low and that is the expected outcome, not a bug.
- When tuning, the question is "are we recalling enough?", measured by
  `radar eval route` against the reader's own 待读 picks
  (`tests/fixtures/eval/picks.jsonl`). A config change that drops a pick
  needs a reason written next to it.

## 2. Data guardrails come before features

v1 lost a day's data to an empty run that overwrote a file, bloated to
5 GB by rewriting large files daily, and reset its dedup state through a
truncated JSON write. The rules that follow exist because of those.

- **Run logs are append-only.** `data/runs/YYYY/<run_id>-<type>.jsonl` is
  written once by `write_jsonl_new`, which refuses to overwrite. Never
  rewrite, reorder or "repair" a run file; a correction is a new run
  (`rescore`) whose newer `scored_at` wins at read time.
- **Nothing derived is committed.** Seen keys, the site, caches: all
  rebuilt from `data/`. If a derived file is wrong, fix the builder.
- **Empty run = no file.** `fetched_total == 0` writes nothing, flags
  `fetched_zero`, and the health gate blocks publishing.
- **Strict identity only.** `radar.core.identity.canonical_key` is the
  one definition of "same paper": DOI / arXiv / PMID / OpenAlex id, with
  the registry rules documented there. No fuzzy matching, ever.
- **Scorer prompts are versioned and never edited.** A change to the
  prompt is a new `prompts/scorer_vN+1.txt`; the old file stays so its
  records can be reproduced. `tests/contracts/test_prompt_files.py` pins
  each file's hash.
- **Every writer workflow** commits with `if: ${{ !cancelled() }}`, runs in
  the `radar-writer` concurrency group, checks out `fetch-depth: 1`, and
  pushes through `scripts/git_push_retry.sh`. The contract test enforces
  it; do not weaken the test to make a workflow pass.
- **Atomic writes everywhere** (`radar.core.atomic`). No bare `open(…, "w")`
  on anything under `data/`.

## 3. Scope

In: arXiv / OpenAlex / PubMed fetching, DeepSeek scoring, the static site,
browser marks + sync + digest, random reading, figure previews.

Out, deliberately: Zotero sync, literature-system exports, weekly reports,
PDF parsing, vector search, any web framework. A dependency that implies
one of those is scope drift; say so instead of adding it.

## 4. Working here

- `pip install -e ".[dev]"` then `pytest` must be green on a clean machine
  with no node, no plotting libraries and no secrets. Keep it that way.
- `radar doctor` before and after a change; `radar daily --dry-run
  --offline --fixture tests/fixtures/fetch/sample.jsonl` to exercise the
  pipeline without a key.
- Every module takes a `DataRoot`; nothing computes a data path at import.
- Secrets live only in Actions secrets and a local `.env` that is
  gitignored. Never print or log them; tests use placeholder values from
  `tests/conftest.py`.
- Record decisions in the commit message and, when they change behaviour,
  in `docs/adr/`. "Deferred" work goes in a GitHub issue, not in prose.
