<!-- generated-by: gsd-doc-writer -->
# Architecture — FIS AI Knowledge Agent

## System Overview

The FIS AI Knowledge Agent is a Databricks Agent Bricks system that turns Fleetworthy Infrastructure Solutions' siloed ServiceNow R&D troubleshooting history into a cited, conversational knowledge agent. Its input is a natural-language question from an R&D/field-support (SOS) engineer facing an incomplete or open task (for example, a WIM reporting zero weights, an AUR camera failing, or an HTS web app crashing). Its output is a cited, actionable recommendation grounded in the most relevant prior cases.

Architecturally it is a **retrieval-and-orchestration** system layered on Unity Catalog. A single canonical Delta table is the source of truth for every R&D case. Two complementary retrieval engines read that table — a **Knowledge Assistant (KA)** for semantic similar-case retrieval with citations, and a **Genie Space** for natural-language-to-SQL over structured columns. A **Multi-Agent Supervisor (MAS)** routes each question to the right engine (or fans out to both) and resolves domain jargon via a `glossary_lookup` Unity Catalog function. A brandable **Databricks One / Genie front door** puts the whole thing in front of the ~10-person SOS team. The system runs entirely on Databricks serverless. Catalog and schema are bundle variables (default `main.troubleshooting_knowledge_agent`, overridable per workspace with `--var catalog=… --var schema=…`); the reference deployment targets `serverless_stable_l26d62_catalog.fis_knowledge_agent` in the `fevm-serverless-stable-l26d62` workspace.

The question workload is characterized by five query archetypes drawn from the customer's acceptance script: terminology matching, domain-expert finding, task complexity/delay analysis, priority triage of open tasks, and site-specific recurring-issue detection. Semantic archetypes route to KA; structured/aggregate archetypes route to Genie; hybrid archetypes fan out to both.

## Logical View

At the logical level there are two ingestion paths off the same ServiceNow source and three inputs into the Supervisor:

```mermaid
graph LR
    SN[ServiceNow<br/>R&D tickets]
    EXTRACT[ai_extract]
    STRUCT[Structured<br/>ticket table]
    GENIE[Genie]
    KA[index / KA]
    GLOSS[Glossary]
    SUP[Supervisor]

    SN --> EXTRACT --> STRUCT --> GENIE
    SN --> KA

    KA --> SUP
    GENIE --> SUP
    GLOSS --> SUP
```

`ai_extract` turns raw tickets into a structured table that **Genie** queries with NL→SQL; the same tickets are indexed for semantic retrieval by the **KA**. The **Supervisor** orchestrates KA, Genie, and the **Glossary** — the glossary being the governed controlled vocabulary. The detailed component diagram below expands each of these into the actual Databricks assets.

## Component Diagram

```mermaid
graph TD
    subgraph Ingestion
        MD[ServiceNow markdown samples<br/>open / incomplete / complete tasks]
        SYN[Synthetic ticket generator<br/>ai_query + Faker taxonomy]
    end

    subgraph "Unity Catalog — the demo schema"
        RAW[rnd_tickets<br/>bronze canonical Delta<br/>case_text + metadata struct + CDF]
        SILVER[rd_tasks_silver<br/>typed + content_hash]
        GOLD[rd_tasks_gold_enrichment<br/>streaming ai_query columns]
        SERVING[rd_tasks_serving<br/>one table both engines read<br/>ka_content + metadata struct + CDF]
        VIEW[rd_tasks_serving_analytics<br/>curated Genie view]
        GLOSS[glossary<br/>approved terms]
        FN[glossary_lookup<br/>UC function]
    end

    subgraph "Retrieval Engines — Agent Bricks"
        KA[Knowledge Assistant<br/>fis-rnd-knowledge-assistant]
        GENIE[Genie Space<br/>FIS R&D Tickets]
    end

    MAS[Multi-Agent Supervisor<br/>routes KA / Genie / glossary_lookup]
    FRONT[Databricks One / Genie front door<br/>~10 SOS users]

    MD --> RAW
    SYN --> RAW
    RAW --> SILVER
    SILVER --> GOLD
    RAW --> SERVING
    SILVER --> SERVING
    GOLD --> SERVING
    SERVING --> VIEW
    GLOSS --> FN
    GLOSS --> GOLD

    SERVING --> KA
    VIEW --> GENIE

    KA --> MAS
    GENIE --> MAS
    FN --> MAS
    MAS --> FRONT
```

Data flows left-to-right: raw ServiceNow markdown plus synthetically generated tickets land in the canonical `rnd_tickets` Delta table; the data-generation step shapes that (real + synthetic) corpus into the `rd_tasks_silver` layer; two **serverless notebook job tasks** then derive the `ai_query` gold enrichment (`enrich.py` — incremental via a `content_hash` anti-join + `MERGE`, so the LLM runs only on new/changed tickets) and the consolidated `rd_tasks_serving` **plain Delta table** (`serving.py`) with its segmented, acronym-expanded content column; the KA and Genie read the enriched artifacts; the Supervisor orchestrates them plus the glossary function; and the front door exposes the Supervisor to end users. Bronze ingest, the silver build, and the SME-governed glossary run as job tasks feeding the enrichment. (The serving table is a plain table, not a materialized view, because the KA streams from it — an MV cannot be streamed.)

## Data Flow

A typical question moves through the system as follows:

1. **Entry.** An SOS engineer asks a question through the Databricks One / Genie front door, which routes to the Multi-Agent Supervisor under a single conversation and the user's own identity and grants.
2. **Terminology resolution.** The Supervisor calls the `glossary_lookup(term_query)` Unity Catalog function first when jargon needs disambiguation (for example, `CA` → Controller Application, `NetBooter` → WPS). It receives the term's canonical definition and `category` and passes the term plus category downstream. The `category` drives how the term is handled: a `system` term is matched as an array element, while a non-system term is matched as free text — the correctness fix that prevents a false zero count.
3. **Routing.** Based on the sub-agent tool descriptions, the Supervisor routes to the correct engine(s):
   - **Semantic archetypes** (terminology matching, similar-case retrieval, cross-site recurring patterns) → Knowledge Assistant.
   - **Structured/aggregate archetypes** (counts, priority sorting, involvement counts) → Genie Space.
   - **Hybrid archetypes** (expert-finding, complexity/delay, priority triage) → both engines, fanned out and merged.
4. **Retrieval.** The KA performs instructed retrieval over the indexed ticket content and returns similar prior cases with inline citations that resolve to the source ticket. Genie generates SQL against the curated analytics view and returns aggregates, ranked lists, or involvement counts.
5. **Synthesis.** The Supervisor merges the sub-agent results into one cited, actionable answer, applying routing/synthesis instructions that shape marquee answers (for example, priority triage classifies open tasks as easiest-with-known-fix / oldest-but-blocked / stubborn-recurring, each with a ticket pointer).
6. **Output.** The answer is returned to the user through the front door with citations that resolve back to real corpus tickets.

## Key Abstractions

The system is composed of Databricks-native assets rather than application source code. The most significant abstractions:

- **Bronze canonical table** — `<catalog>.<schema>.rnd_tickets`. One row per R&D case, holding the full `case_text`, typed metadata columns, and the `metadata` STRUCT, with Change Data Feed enabled. It is the source of truth the silver/gold/serving layers derive from.
- **The one serving table** — `rd_tasks_serving`, the single physical table **both** engines read: the KA-indexed `ka_content` column, the `metadata` STRUCT, and CDF enabled. It is a **plain Delta table** (not a view or materialized view) so the Knowledge Assistant can *stream* from it — streaming from an MV is unsupported. Built in-job by the `serving` notebook (`src/notebooks/serving.py`).
- **Enrichment gold layer** — `rd_tasks_gold_enrichment` (a Delta table, incrementally `MERGE`-upserted, gated by `content_hash`). `ai_query`-driven enrichment adds canonical structured columns (`systems_involved`, `hardware`, `vendors`, `problem_category`, `root_cause`, `resolution_type`) plus a segmented description (`summary` / `customer_impact` / `troubleshooting` / `recommendation`). Genie's read surface is the curated view `rd_tasks_serving_analytics` (with `rd_tasks_gold_analytics` kept as a compatibility alias) over `rd_tasks_serving`.
- **Governed glossary** — the `glossary` table (canonical term, definition, category, synonyms/aliases, approved_by, version) and the `glossary_lookup(term_query STRING) RETURNS TABLE(term STRING, definition STRING, category STRING)` Unity Catalog function. Sourced from product docs (authoritative) merged with ServiceNow usage evidence; only approved terms are served. This is the controlled vocabulary that drives both enrichment enums and the Supervisor's terminology resolution.
- **Knowledge Assistant (KA)** — `fis-rnd-knowledge-assistant`, served at endpoint `ka-97df484b-endpoint`. An Agent Bricks Instructed Retriever indexing `rd_tasks_serving.ka_content` plus a glossary Volume source; returns similar-case answers with resolving citations. Queried via `POST /serving-endpoints/{endpoint}/invocations`.
- **Genie Space** — `FIS R&D Tickets` (space_id `01f185f0ce8e15cd9a92d86b3171c52e`). Natural-language-to-SQL over `rd_tasks_serving_analytics`, encoding involvement counting, open-task priority ranking, and delay/complexity signals as certified queries and instructions.
- **Multi-Agent Supervisor (MAS)** — orchestrates the three tools (KA endpoint, Genie space, `glossary_lookup` function) under one conversation. Routing is driven by sharp, disambiguating natural-language tool descriptions rather than hard-coded rules. Supervisor LLM is `databricks-claude-sonnet-4-5`. <!-- VERIFY: MAS serving endpoint id and routing-instruction contents — Agent Bricks MAS metadata is not exposed via the serving API and must be read from the Agent Bricks UI -->
- **Foundation Model API endpoints** — `databricks-claude-sonnet-4-5` powers the agents (synthesis, reasoning) and drives `ai_query` enrichment; `databricks-claude-haiku-4-5` powers the synthetic-ticket generation pass.
- **Front door** — Databricks One (with Genie as the fallback front door) provides the brandable entry point for the SOS users and routes to the Supervisor. <!-- VERIFY: Databricks One GA availability and consumer-access entitlement state in fevm-serverless-stable-l26d62 -->

## Directory Structure Rationale

This repository is a Databricks Asset Bundle — the running system lives as Databricks
assets, not as an application source tree. The bundle root (`databricks.yml`) includes
`resources/*.yml`; the scripts and pipeline code the resources point at live under `src/`,
`data_generation/`, `genie/`, and `frontdoor/`. Top-level layout:

```
.
├── databricks.yml            Bundle definition (variables, targets, sync excludes)
├── resources/                DAB resources: uc.yml, genie.yml, apps.yml,
│                             jobs_pipeline.yml, jobs_agents.yml
├── src/
│   ├── notebooks/            Serverless notebook tasks: enrich.py (gold enrichment) +
│   │                         serving.py (plain-Delta rd_tasks_serving + analytics + verify);
│   │                         enrich_recipe.py is the shared, I/O-free recipe they import.
│   ├── deploy/               Job-task scripts: parse_tickets, load_tables, build_glossary,
│   │                         build_serving_table (LOCAL analytics + verify CLI), build_serving_agents
│   │                         (KA), build_supervisor (MAS), frontdoor_deploy, env, preflight,
│   │                         and the test_* harnesses + json/md inputs
│   └── pipeline/
│       └── transformations/  Lakeflow pipeline: enrich_recipe (shared recipe),
│                             gold_enrichment (streaming ai_query), serving (MV, CDF)
├── data/servicenow/          The ticket corpus markdown (parse_tickets reads it); ships
│                             WITH the repo so the bundle is self-contained
├── data_generation/          build_silver.py (silver layer) + generate.py (synthetic corpus)
├── genie/                    genie_space.json (native DAB genie_spaces payload)
├── frontdoor/                FastAPI + built SPA front-door app (deployed via apps.yml)
├── eval/                     MLflow GenAI evaluation harness (run_eval.py)
├── specifications/           Component specs (01 ingest+enrich, 02 agents, 03 apps)
└── DEPLOYMENT.md / dab_instructions.md / architecture.md / README.md
```

- **`resources/` + `databricks.yml`** — the bundle. Native resources (UC schema/volume,
  the `genie_spaces` Genie space, the front-door app, and the enrich pipeline) plus the jobs
  that run the imperative build scripts DAB has no resource type for.
- **`src/deploy/`** — the job-task scripts: bronze ingest (`parse_tickets` → `load_tables`),
  the governed `glossary` + `glossary_lookup` builder, the analytics-view + `--verify` step
  (`build_serving_table.py`), and the Agent Bricks builds DAB cannot express natively
  (`build_serving_agents.py` for the KA, `build_supervisor.py` for the MAS) with their
  `test_*.py` isolation harnesses and glossary/example inputs. The Genie Space is NOT
  script-built — it is the native `genie_spaces` resource (`resources/genie.yml`) whose
  payload is rendered per catalog/schema by `src/deploy/render_genie.py` from
  `genie/genie_space.template.json`; `test_genie.py` exercises it.
- **`src/notebooks/`** — the two serverless notebook job tasks that derive the gold layer:
  `enrich.py` builds `rd_tasks_gold_enrichment` (LLM `ai_query`, incremental via a
  `content_hash` anti-join + `MERGE`) and `serving.py` builds the `rd_tasks_serving` **plain
  Delta table** both engines read (then the analytics views + `verify()`). `enrich_recipe.py`
  is the single-source, I/O-free recipe (ai_query schema + acronym expansion) they both import.
- **`data_generation/`** — `build_silver.py` shapes bronze into `rd_tasks_silver`; `generate.py`
  is the one-time synthetic-corpus authoring tool (its output is pre-generated markdown that
  `parse_tickets` reads — not wired into the deploy job).
- **`frontdoor/`** — the front-door app (FastAPI + built SPA) uploaded via
  `resources/apps.yml`; OBO scopes are bound post-deploy by `src/deploy/frontdoor_deploy.py`.
- **`eval/`** — the re-runnable MLflow GenAI evaluation harness (`run_eval.py`) that scores
  correctness, relevance, and citation-groundedness.
