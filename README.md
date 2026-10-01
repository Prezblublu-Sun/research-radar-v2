# Research Radar v2

A daily paper radar: fetch new work from arXiv, OpenAlex and PubMed, dedup
by exact identity, route to six research directions by keyword rules, score
each paper with DeepSeek against that direction's focus, and publish a static
site with reading marks that sync back through GitHub.

Six directions (see `config/directions.yaml`): geometry-adaptive operator
learning, FE-flavoured physics-informed networks, surrogate modelling with
uncertainty quantification, data-efficient learning, the design loop, and
the femoral-stem / implant biomechanics domain anchor.

## Install

```bash
pip install -e ".[dev]"
pytest
```

That is the whole onboarding. The test suite needs no node, no plotting
libraries and no API keys.

## Run

```bash
radar doctor                                   # offline health check
radar eval route                               # router recall on the 121 hand-picked papers
radar daily --dry-run --offline --fixture tests/fixtures/fetch/sample.jsonl
radar daily --dry-run                          # real fetch, no scoring, no key needed
radar daily                                    # needs OPENAI_API_KEY (DeepSeek)
```

A dry run writes under `.radar-dryrun/` and prints the per-direction routing
table. A real run appends one file, `data/runs/YYYY/<run_id>-daily.jsonl`:
line 1 is the run header (config and prompt hashes, source status, token
usage, counts, quality flags); every later line is one paper.

## Layout

```
config/directions.yaml   the six directions, their fetch terms and routing rules
prompts/scorer_vN.txt    scorer prompts, versioned, never edited in place
radar/                   the package: core/ sources/ pipeline/ store/ site/ …
data/runs/               append-only run logs (the only data that is committed)
tests/                   unit, contract and eval suites; fixtures for offline runs
```

Environment variables: `OPENAI_API_KEY` (DeepSeek key), `OPENAI_BASE_URL`
(default `https://api.deepseek.com`), `MODEL_NAME` (default
`deepseek-v4-flash`), `OPENALEX_API_KEY`, `PUBMED_EMAIL`.

See `CLAUDE.md` for the working agreement and the data guardrails.
