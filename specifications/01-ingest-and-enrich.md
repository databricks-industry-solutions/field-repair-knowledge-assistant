# Ingest + Enrichment — raw tickets to one serving table

## Shared Context

**Demo:** R&D Troubleshooting Knowledge Assistant. A decade of roadside
truck-screening support tickets becomes a cited, actionable agent. Every
downstream consumer (Knowledge Assistant, Genie, Supervisor) reads **one physical
table**, `rd_tasks_serving`.

**Target:** `{{CATALOG}}.{{SCHEMA}}` (defaults `dbdemos_templates.rnd_knowledge_agent`).

**Build shape (no SDP pipeline).** These are SQL-over-REST drivers, not Spark
transformations: each script builds SQL strings and issues them through the SQL
Statements API against a serverless warehouse. That is why the bundle wires them as
ordered **job tasks** rather than a `pipelines` resource — there is no streaming
table and no Spark session, so a Lakeflow pipeline would be the wrong abstraction.

**Grain:** one row per task, end to end. Bronze, silver, enrichment and the serving
table all carry the same row count and join 1:1 on `number`. The serving-table
build asserts this (`count(*) - count(DISTINCT number) == 0`) so a join fan-out
cannot silently inflate the corpus.

---

## A. Bronze — parse the tickets

`src/deploy/parse_tickets.py` → `src/deploy/load_tables.py`

The source tickets are markdown, so they are parsed with plain string/regex work.
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

`src/deploy/silver.py`

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

`src/deploy/enrich.py`

One structured-extraction pass over every ticket not already enriched
(`silver LEFT ANTI JOIN gold ON content_hash` — so a re-run with unchanged tickets
does zero LLM work and costs nothing).

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
   `glossary WHERE status='approved'` at run time and injected into the extraction
   schema. The build refuses to run if no approved system terms exist, rather than
   emitting an uncontrolled term list. A `--drift-guard` mode asserts the
   enrichment's system values and the approved glossary set are identical in both
   directions.
2. **Never invent content.** The model is instructed to return an empty string for
   any description part the ticket does not cover. A fabricated "customer impact"
   is worse than a blank one.

**Materialize once.** The extraction output is written to a staging table before
the MERGE. Referencing an LLM output column twice would compute — and bill — it
twice.

An alternative implementation using `ai_extract` instead of `ai_query` is included
as a notebook; it is simpler to read (the schema is the API, and confidence scores
come back natively on 0–1) but the two paths write the same columns.

## E. The one serving table

`src/deploy/build_serving_table.py` → `rd_tasks_serving` + `rd_tasks_serving_analytics`

Joins silver ⋈ enrichment ⋈ ticket text into a single physical table carrying
everything both engines need, plus a pre-composed content column and the metadata
struct. `TBLPROPERTIES (delta.enableChangeDataFeed = true)`.

**It must be a TABLE, not a view.** A view cannot back a Knowledge Assistant:
attaching one fails with *"must either be a streaming table or have Change Data
Feed enabled"*, and CDF cannot be set on a view.

The content column is composed here rather than left to the agent because
`file_col` accepts **exactly one** column (`Array must have size 1, but has size 2`).
Acronyms are expanded inline from the approved glossary — `AUR` becomes
`AUR (Camera unit that reads USDOT numbers…)` — so a retrieval query using the
acronym and one using the expansion both hit.

`--verify` runs 11 assertions: row count, metadata struct present, CDF on, no
empty content, every row citeable, acronym expansion applied, grain unique, and
content parity with the previous surface.
