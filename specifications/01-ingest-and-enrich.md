# Ingest + Enrichment — raw tickets to one serving table

## Shared Context

**Demo:** R&D Troubleshooting Knowledge Assistant. A decade of roadside
truck-screening support tickets becomes a cited, actionable agent. Every
downstream consumer (Knowledge Assistant, Genie, Supervisor) reads **one physical
table**, `rd_tasks_serving`.

**Target:** `{{CATALOG}}.{{SCHEMA}}` — the bundle default is
`main.troubleshooting_knowledge_agent` (`databricks.yml`); override per workspace with
`--var catalog=… --var schema=…`.

**Build shape.** Bronze (`parse_tickets`/`load_tables`), the silver layer
(`data_generation/build_silver.py`), and the SME-governed `glossary` are
SQL-over-REST job tasks. `build_silver.py` shapes bronze `rnd_tickets` (real +
synthetic) into `rd_tasks_silver` (+ `rd_task_note_entries`) — the `content_hash`
gate + location parse, CDF on. The **enrich chain — gold_enrichment → serving — is two
serverless `notebook_task` notebooks** in the same job, `src/notebooks/enrich.py` and
`src/notebooks/serving.py`; they read `rd_tasks_silver`, `glossary` and `rnd_tickets`.
`rd_tasks_gold_enrichment` is a Delta table built **incrementally**: a `content_hash`
LEFT ANTI JOIN selects only new/changed tickets, `ai_query` runs over just those, and a
`MERGE` upserts by `number` — so a re-run with no new tickets does zero LLM work.
`rd_tasks_serving` is a **plain Delta table** (CDF on), NOT a materialized view: the KA
streams from it and streaming from an MV is unsupported. `glossary` mines from bronze
`rnd_tickets`, not silver, so the approved vocabulary (GLO-02) is ready before the
enrichment enum is built.

**Grain:** one row per task, end to end. Bronze, silver, enrichment and the serving
table all carry the same row count and join 1:1 on `number`. The serving-table
build asserts this (`count(*) - count(DISTINCT number) == 0`) so a join fan-out
cannot silently inflate the corpus.

---

## A. Bronze — parse the tickets

`src/deploy/parse_tickets.py` → `src/deploy/load_tables.py`

The source ticket markdown ships with the repo under `data/servicenow/` (so the bundle
is self-contained and the pipeline runs in any workspace); `parse_tickets.py` resolves
that path relative to itself, overridable via `FIS_SAMPLE_DIR`. The source tickets are
markdown, so they are parsed with plain string/regex work.
**Do not** reach for `ai_parse_document` here: the text is already text, and OCR
would be both slower and region-gated.

Produces the ticket table plus a note-entry table (one row per dated note), which
later becomes a difficulty proxy — a ticket with fifteen note entries was harder
than one with two.

Each ticket row carries a `metadata` STRUCT:

```
struct<file_path:string, file_name:string, file_size:bigint, file_modification_time:timestamp>
```

**This struct is load-bearing, not decoration.** It is what lets the Knowledge
Assistant treat a row as a citeable document. A table without it is rejected at
attach time with `missing required column '_metadata'`.

## B. Silver — typed and derived

`data_generation/build_silver.py`

Types the columns and derives what Genie needs to filter and sort: priority as an
integer (1 = highest), `is_closed`, the location split into state / highway /
site / a canonical `site_key`, `duration_days`, and activity counts. Also computes
`content_hash` over the ticket text — the gate that makes enrichment incremental.

## C. Glossary — the controlled vocabulary

`src/deploy/build_glossary.py`

Builds the governed glossary table. A term carries a `category`
(`system` / `software` / `hardware` / `vendor` / `process`) and a `status`. Only
`status='approved'` terms participate downstream.

The category is the mechanism behind a correctness fix worth demoing: `CA` is
category `software`, so it must be matched in text columns, never with
`array_contains(systems_involved, 'CA')` — which returns a confident **zero**.

## D. Gold — LLM enrichment

`src/notebooks/enrich.py` (+ the shared, I/O-free recipe in
`src/notebooks/enrich_recipe.py`)

`rd_tasks_gold_enrichment` is a Delta table built **incrementally**: a `content_hash`
LEFT ANTI JOIN selects only silver rows not already enriched, `ai_query` runs over just
those into a materialized stage table, and a `MERGE` upserts by `number`. A re-run with
unchanged tickets selects zero rows, so `ai_query` makes zero model calls and the MERGE
is skipped — zero LLM cost. (A changed ticket's `content_hash` changes, so it is picked
up and re-enriched on the next run.)

Extracts:

| Field | Type | Notes |
|---|---|---|
| `systems_involved` | ARRAY&lt;STRING&gt; | enum, **built at run time from the approved glossary** |
| `vendors` | ARRAY&lt;STRING&gt; | enum, same source |
| `hardware_mentioned` | ARRAY&lt;STRING&gt; | free text; anything not in the enums lands here |
| `problem_category` | STRING | fixed taxonomy (hardware_failure, software_crash, …) |
| `summary` / `customer_impact` / `troubleshooting` / `recommendation` | STRING | the description segmented **by meaning**, empty string where the ticket does not cover a part |
| `root_cause` / `resolution` / `resolution_type` | STRING | causal fields |
| `conf_*`, `min_confidence`, `needs_review` | DOUBLE / BOOLEAN | low confidence flags the row for expert review |

Two rules that matter:

1. **No hardcoded vocabulary.** The systems/vendors enums are read from
   `glossary WHERE status='approved'` at update time and injected into the extraction
   schema. The pipeline refuses to emit an uncontrolled term list, and a pipeline
   **expectation** asserts every emitted `systems_involved` value is an approved
   `category='system'` term — the declarative replacement for the old `--drift-guard`
   mode (GLO-02).
2. **Never invent content.** The model is instructed to return an empty string for
   any description part the ticket does not cover. A fabricated "customer impact"
   is worse than a blank one.

**Materialize once.** The `ai_query` output is materialized in a private pipeline
table (`@dp.table`) before it is unpacked into columns. Referencing an LLM output
column twice would compute — and bill — it twice.

## E. The one serving table

`src/notebooks/serving.py` builds `rd_tasks_serving` (plain Delta, CDF on) then the
curated Genie views `rd_tasks_serving_analytics` (+ the `rd_tasks_gold_analytics` compat
alias) and runs `verify()` in-job. For a local re-check off a laptop,
`src/deploy/build_serving_table.py --verify` runs the same serving-table assertions
(and `--analytics-only` re-creates the views).

The `serving` notebook joins silver ⋈ enrichment ⋈ ticket text into a single physical
table carrying everything both engines need, plus a pre-composed content column and the
metadata struct, with `delta.enableChangeDataFeed = true` set as a table property.

**It must be a plain Delta TABLE — not a view, and not a materialized view.** The KA
sync performs a *streaming* read of its source, so the source has to be streamable: a
plain Delta table (with CDF) or a streaming table. A plain view cannot back a KA at all,
and a **materialized view fails too** — streaming from an MV raises
`STREAMING_FROM_MATERIALIZED_VIEW` *even with CDF enabled* (CDF exposes the change feed
but does not make an MV a streamable source). That is why the serving table is built by a
job notebook as `CREATE OR REPLACE TABLE … TBLPROPERTIES(delta.enableChangeDataFeed=true)`
rather than as a Lakeflow pipeline dataset, whose natural output for this join is an MV.

The content column is composed here rather than left to the agent because
`file_col` accepts **exactly one** column (`Array must have size 1, but has size 2`).
Acronyms are expanded inline from the approved glossary — `AUR` becomes
`AUR (Camera unit that reads USDOT numbers…)` — so a retrieval query using the
acronym and one using the expansion both hit.

`--verify` runs 11 assertions: row count, metadata struct present, CDF on, no
empty content, every row citeable, acronym expansion applied, grain unique, and
content parity with the previous surface.
