# FIS v2 — Build Plan

## Goal
Build a custom retrieval agent backed by Lakebase (pgvector + full-text) indexes over R&D task knowledge content, served via a Databricks App. The agent combines three capabilities:
1. **Knowledge search** — hybrid vector + full-text retrieval over R&D task tickets in Lakebase
2. **Glossary lookup** — domain-term resolution from a curated glossary table (94 terms) so the agent can understand FIS-specific acronyms and equipment names
3. **Genie (quantitative Q&A)** — routes data/analytics questions (counts, rankings, aggregations) to a Genie space for SQL-backed answers

---

## Source Data Summary

| Property | Value |
| --- | --- |
| Table | `serverless_stable_l26d62_catalog.fis_knowledge_agent.rd_tasks_serving` |
| Total rows | 223 |
| Rows with `ka_content` | 223 (100%) |
| Avg content length | ~960 chars |
| Content type | Structured ticket narratives: problem, troubleshooting, root cause, resolution |
| Lakebase project | `fis` (already exists, `production` branch) |
| Glossary table | `serverless_stable_l26d62_catalog.fis_knowledge_agent.glossary` — 94 approved terms, 6 categories (system, vendor, software, process, hardware, concept) |
| Genie space | `01f190db953e1140b39d10f49a46aa7b` — quantitative analytics over FIS site/task data |

---

## Execution Plan

### Phase 1 — Lakebase Table & Indexes

**Step 1.1: Inspect existing Lakebase project**
- Verify `projects/fis` branch (`production`) and endpoint (`primary`) are READY
- Get the endpoint host and default database name

**Step 1.2: Create the Lakebase table**
- Connect to `projects/fis/branches/production` via psycopg
- Enable `pgvector` extension: `CREATE EXTENSION IF NOT EXISTS vector`
- Create a table with:
  - `number` TEXT PRIMARY KEY — ticket ID from source
  - `title` TEXT — ticket title
  - `lakebase_text` TEXT — the raw `ka_content` for full-text search
  - `lakebase_vector` VECTOR(1024) — embedding of `ka_content`
  - Plus metadata columns as needed (status, priority, location, etc.)

**Step 1.3: Generate embeddings & ingest**
- Read source data from UC table via Spark
- Call `databricks-gte-large-en` (1024-dim) endpoint to embed each `ka_content`
- Batch-insert rows into Lakebase via psycopg

**Step 1.4: Create indexes**
- **Vector index**: `CREATE INDEX ON fis_tasks USING ivfflat (lakebase_vector vector_cosine_ops)` for similarity search
- **Text index**: `CREATE INDEX ON fis_tasks USING gin (to_tsvector('english', lakebase_text))` for full-text keyword search

---

### Phase 2 — Custom Agent (3 tools)

Ref: https://docs.databricks.com/aws/en/agents/custom-agents/author-agent

**Step 2.1: Tool — `fis_knowledge_search` (Lakebase hybrid retriever)**
- Accepts a user query string + optional filters (`location_state`, `status_filter`)
- Embeds via `databricks-gte-large-en` (1024-dim)
- Runs hybrid query: 0.7 × vector cosine + 0.3 × full-text `ts_rank`, TOP_K=5
- Returns ranked R&D task tickets with metadata, root cause, and resolution

**Step 2.2: Tool — `glossary_lookup` (domain-term resolver)**
- Source: `serverless_stable_l26d62_catalog.fis_knowledge_agent.glossary`
- 94 approved terms across 6 categories: system, vendor, software, process, hardware, concept
- Columns: `term`, `definition`, `aliases` (array), `category`, `source_refs`
- Behavior:
  - Agent calls this when it encounters an unknown acronym or FIS-specific term (e.g., "OVC", "PIPS", "Kistler", "AUR")
  - Fuzzy-matches the term against `term` and `aliases` columns
  - Returns the definition, category, and aliases to inform the agent's answer
- Implementation: SQL query against the UC table (read via Spark or direct UC SQL), no Lakebase needed

**Step 2.3: Tool — `genie_query` (quantitative analytics)**
- Genie space: `01f190db953e1140b39d10f49a46aa7b`
  URL: https://fevm-serverless-stable-l26d62.cloud.databricks.com/genie/rooms/01f190db953e1140b39d10f49a46aa7b
- Routes quantitative/analytical questions to Genie for SQL-backed answers
- Use cases: "how many sites have the highest brake failures", "which state has the most open tasks", "show task counts by priority"
- The agent should detect when a question requires counting, ranking, aggregation, or statistical analysis and route to Genie instead of (or in addition to) the knowledge search
- Implementation: Genie Conversation API — start a conversation, send the question, poll for the response

**Step 2.4: Author the agent (LangGraph)**
- Use `ChatDatabricks` (e.g., `databricks-claude-sonnet-4-5`) as the LLM
- Bind all three tools: `fis_knowledge_search`, `glossary_lookup`, `genie_query`
- System prompt:
  - For qualitative questions (troubleshooting, root cause, resolution) → use `fis_knowledge_search`
  - For unknown terms or acronyms → use `glossary_lookup` to resolve before answering
  - For quantitative questions (counts, rankings, aggregations, "how many") → use `genie_query`
  - The agent may chain tools: e.g., glossary to understand a term, then knowledge search with the resolved meaning
- Wrap in a LangGraph `create_react_agent`

**Step 2.5: Log & register the agent**
- Log with `mlflow.langchain.log_model()`
- Register to Unity Catalog as a model
- Deploy to a model serving endpoint

---

### Phase 3 — Databricks App (Frontend)

**Step 3.1: Scaffold the app**
- `apps init --name fis-v2 --features lakebase`
- Wire to `projects/fis/branches/production` database

**Step 3.2: Build chat UI**
- Use AppKit or Gradio for a conversational interface
- Connect to the agent serving endpoint via REST
- Display retrieved context alongside the agent's answer

**Step 3.3: Deploy**
- `apps deploy fis-v2`
- Grant access to the team

---

## Artifacts to Create

| # | Asset | Type | Purpose |
| --- | --- | --- | --- |
| 1 | `fis_v2_ingest` | Notebook | Lakebase table creation, embedding, and ingestion |
| 2 | `fis_v2_agent` | Notebook | Agent definition (3 tools), testing, MLflow logging |
| 3 | `fis-v2` | App | Chat frontend for the agent |
| 4 | `fis_v2_tests` | Notebook | Test suite: connectivity, embeddings, search, agent e2e, glossary, Genie |

---

## Status

| Phase | Status | Notes |
| --- | --- | --- |
| Phase 1 — Lakebase Table & Indexes | ✅ Complete | 223 rows, pgvector + GIN indexes, Lakebase role for app SP |
| Phase 2 — Custom Agent (3 tools) | ✅ Complete | fis_knowledge_search, glossary_lookup, genie_query in LangGraph |
| Phase 3 — Databricks App | ✅ Complete | Gradio app deployed as fis-v2, all 3 tools live |
| Testing | ✅ Complete | 8 suites, 35+ tests, all passing |

## Resolved Questions

1. ~~Metadata columns~~ → `location_state` and `status` as optional filters
2. ~~Structured vs. semantic~~ → Both, via optional tool params
3. ~~Hybrid search weighting~~ → 0.7 vector + 0.3 text
4. ~~Glossary pre-load vs. query UC~~ → Pre-load into memory (94 terms, notebook uses Spark, app uses SQL Statement Execution API)
5. ~~Show generated SQL from Genie~~ → Yes, included in tool output
6. ~~Genie timeout handling~~ → 90s timeout with polling, graceful error message on failure
