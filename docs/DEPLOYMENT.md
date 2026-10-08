# DEPLOYMENT — v2 agent runbook

> **Audience: an AI agent (or engineer) deploying this template unattended.** Follow
> the stages in order. Every stage has a **GATE** you must pass before continuing. If
> a gate fails, stop and report — do not proceed to the next stage, and do not paper
> over a failure by re-running blindly.
>
> **Rules for this runbook**
> 1. Never report success you have not observed. Paste the actual command output.
> 2. A check that prints its own `PASS`/`VERIFY PASSED` line is evidence. Your own
>    reasoning is not.
> 3. If a step is skipped, say which one and why.
> 4. Auth can expire mid-run — see the auth note in Stage 0.

> [!NOTE]
> **This is the v2 runbook** (Lakebase + custom LangGraph agent). The earlier v1
> Agent Bricks flow (Knowledge Assistant + Multi-Agent Supervisor + OBO front-door
> app) is gone. For the architecture, see **[ARCHITECTURE.md](ARCHITECTURE.md)**;
> the v2-specific companion docs live under
> **[`v2/docs/`](../v2/docs/DEPLOYMENT_V2.md)**.

---

## Overview — what you deploy

The deploy has four stages:

1. **Data pipeline** (the existing Databricks Asset Bundle) — ingest the ticket corpus,
   run the `ai_query` enrichment, and stand up the **Genie space**. This produces the
   enriched rows that both the Lakebase retriever and Genie read.
2. **Lakebase** — create the `fis_tasks` Postgres table with its `pgvector` and GIN
   indexes, then embed and load the enriched rows into it.
3. **Agent test** — run the v2 test notebook against the live Lakebase + Genie +
   glossary before exposing anything to users.
4. **Gradio app** — deploy the `fis-v2` Databricks App (which runs the LangGraph agent
   in-process) and grant its service principal a Lakebase role.

**Requirements.**

| Requirement | Detail |
|---|---|
| Databricks CLI | **v1.3.0+** (genie_spaces + app resource bindings) |
| Serverless SQL warehouse | AI Functions do **not** run on SQL Warehouse Classic |
| Lakebase project | project `fis`, branch `production`, endpoint `primary` (adjust to yours) |
| Serving endpoints | `databricks-gte-large-en` (embedding, 1024-dim) and `databricks-claude-sonnet-4-5` (LLM) |
| Genie space | a configured space id for the analytics tool |
| Unity Catalog | a schema holding the enriched rows + the approved `glossary` table |

**Where config lives.** The v2 code carries its connection config as constants in
**two** places that must agree: `v2/agent/fis_v2_agent.py` (the notebook) and
`v2/app/app.py` (the app). The table below is the reference set (defaults from the
shipped example — change them for your workspace):

| Parameter | Example value |
|---|---|
| `LAKEBASE_HOST` | `ep-long-feather-d20bt18w.database.us-east-1.cloud.databricks.com` |
| `LAKEBASE_DB` | `databricks_postgres` |
| `LAKEBASE_TABLE` | `fis_tasks` |
| `EMBEDDING_MODEL` | `databricks-gte-large-en` |
| `LLM_ENDPOINT` | `databricks-claude-sonnet-4-5` |
| `GLOSSARY_TABLE` | `<catalog>.<schema>.glossary` |
| `GENIE_SPACE_ID` | `01f190db953e1140b39d10f49a46aa7b` |
| `TOP_K` | `5` |

---

## Stage 0 — Preconditions

```bash
# 0.1 Authenticate. Interactive: a human must run this if it fails.
databricks auth login --profile <PROFILE>

# 0.2 Prove the token works AND resolves to the intended workspace.
databricks auth env --profile <PROFILE> | head -5
databricks current-user me --profile <PROFILE>

# 0.3 CLI version.
databricks --version          # need v1.3.0+

# 0.4 Confirm a SERVERLESS SQL warehouse exists and is the one you will pass.
databricks warehouses get <WAREHOUSE_ID> --profile <PROFILE> | grep -E '"name"|"enable_serverless_compute"|"state"'

# 0.5 Confirm the Lakebase endpoint resolves.
databricks postgres get-endpoint \
  --name projects/fis/branches/production/endpoints/primary --profile <PROFILE> \
  | grep -E '"host"|"state"'

# 0.6 Confirm the serving endpoints exist.
databricks serving-endpoints get databricks-gte-large-en   --profile <PROFILE> | grep '"state"'
databricks serving-endpoints get databricks-claude-sonnet-4-5 --profile <PROFILE> | grep '"state"'
```

**GATE 0** — all succeed, `enable_serverless_compute` is `true`, and the Lakebase
endpoint + both serving endpoints are healthy.

> **Auth model.** Notebook and local runs authenticate via the Databricks SDK: ambient
> credentials on serverless compute, and the `--profile` you pass when running scripts
> locally. Lakebase connections mint a short-lived token
> (`w.postgres.generate_database_credential(...)`) at connect time.

> **Auth expiry is a real failure mode.** OAuth refresh tokens can expire mid-run. If a
> Lakebase call suddenly fails with `password authentication failed`, re-check
> `databricks auth token --profile <PROFILE>` **before** concluding the table or role
> is broken.

---

## Stage 1 — Data pipeline (enriched rows + Genie)

This is the existing Asset Bundle pipeline; v2 reuses it to produce the enriched rows
and the Genie space. It stops short of the retired v1 agents/app resources.

```bash
# Render the Genie space payload for your catalog/schema (DAB does not interpolate
# inside the genie JSON). Writes git-ignored agents/genie/genie_space.json from the template.
python3 agents/render_genie.py --catalog <CATALOG> --schema <SCHEMA>

databricks bundle validate -t <TARGET>

# Inspect the plan BEFORE mutating anything.
databricks bundle plan -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID>

# Deploy the data infra + Genie space (NOT the retired v1 agents/app resources).
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --select schemas.rkb --select volumes.glossary \
  --select jobs.rkb_data_pipeline

# Run the pipeline: parse_tickets -> load_tables -> {build_silver, glossary} -> enrich -> serving.
databricks bundle run rkb_data_pipeline -t <TARGET>

# The analytics view now exists, so deploy the Genie space.
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --select genie_spaces.rkb_serving
```

`enrich` builds `rd_tasks_gold_enrichment` with `ai_query`, **incremental** via a
`content_hash` anti-join + `MERGE` (LLM runs only on new/changed tickets). `serving`
composes the enriched serving rows (`summary` / `customer_impact` / `troubleshooting`
/ `recommendation`, plus `problem_category` / `root_cause` / `resolution_type`) and the
Genie analytics view. These enriched columns are the **source for both** the Lakebase
load (Stage 2) and the Genie space.

> [!IMPORTANT]
> v2 no longer builds a streamable KA source table. The `_metadata`-struct / CDF /
> "plain table not MV" constraints that governed v1's serving table **do not apply** to
> the Lakebase path — Lakebase is loaded by a plain read of the enriched rows.

**GATE 1** — the enriched rows and glossary exist:

```bash
# Enriched rows present.
databricks sql query --warehouse-id <WAREHOUSE_ID> --profile <PROFILE> \
  -q "SELECT count(*) FROM <CATALOG>.<SCHEMA>.rd_tasks_gold_enrichment"

# Glossary has approved terms (the agent loads these at startup).
databricks sql query --warehouse-id <WAREHOUSE_ID> --profile <PROFILE> \
  -q "SELECT count(*) FROM <CATALOG>.<SCHEMA>.glossary WHERE status='approved'"
```

Both counts must be non-zero. An empty glossary means the agent's `glossary_lookup`
tool comes up empty at startup.

---

## Stage 2 — Lakebase (build & load `fis_tasks`)

Create the Lakebase project once (skip if it exists):

```bash
databricks postgres create-project fis --json '{"spec":{"display_name":"fis","pg_version":17}}' --profile <PROFILE>
```

The table itself is built by the data job. Its `lakebase_sync` task
(`lakebase/lakebase_sync.py`) joins the Lakeflow silver table with location and
enrichment, embeds only new or changed tickets with `databricks-gte-large-en`, and
upserts them into `fis_tasks`, creating the pgvector column, the primary key, and the
GIN full-text and IVFFlat indexes if they're missing. So Stage 1's `bundle run
rkb_data_pipeline` already loaded it.

**GATE 2** — the table is populated and both indexes exist:

```sql
SELECT count(*) FROM fis_tasks;                 -- must be > 0 and match the enriched row count
SELECT count(*) FROM fis_tasks WHERE lakebase_vector IS NOT NULL;  -- must equal the row count
SELECT indexname FROM pg_indexes WHERE tablename = 'fis_tasks';    -- expect idx_fis_tasks_vector + idx_fis_tasks_text
```

---

## Stage 3 — Test the agent

Run the v2 test notebook end-to-end against the live Lakebase + Genie + glossary.

```
v2/tests/fis_v2_tests.py — 8 suites, 35+ tests:
  Suite 1: Lakebase Connectivity (6)
  Suite 2: Embedding Model (4)
  Suite 3: Vector Search (4)
  Suite 4: Retriever Tool (4)
  Suite 5: LangGraph Agent — 3 tools (4)
  Suite 6: Edge Cases & Data Quality (5)
  Suite 7: Glossary Lookup (8)
  Suite 8: Genie Query (3)
```

**GATE 3** — every suite passes. A failure in Suite 1/2 is infra (Lakebase or the
embedding endpoint); 3/4 is retrieval; 5 is agent wiring; 7/8 are the glossary/Genie
tools. Do not deploy the app on a red suite.

---

## Stage 4 — Deploy the Gradio app

The app (`v2/app/`) runs the LangGraph agent in-process behind a service principal.
Workspace settings come from `app.yaml` env: set `RKB_CATALOG` and `RKB_SCHEMA` to the
values you deployed the bundle with. `GENIE_SPACE_ID` comes from the `genie-space`
app resource, and the Lakebase host is looked up from `LAKEBASE_ENDPOINT` at startup.

```bash
# 4.1 Create the app and read its service principal id.
databricks apps create --json '{"name":"fis-v2"}' --profile <PROFILE>
databricks apps get fis-v2 --profile <PROFILE> --output JSON | jq -r .service_principal_client_id

# 4.2 Attach resources (the platform grants the SP access to each).
databricks apps update fis-v2 --profile <PROFILE> --json '{"name":"fis-v2","resources":[
  {"name":"genie-space","genie_space":{"name":"Field Repair Tickets (serving)","space_id":"<GENIE_SPACE_ID>","permission":"CAN_RUN"}},
  {"name":"sql-warehouse","sql_warehouse":{"id":"<WAREHOUSE_ID>","permission":"CAN_USE"}},
  {"name":"llm","serving_endpoint":{"name":"databricks-claude-sonnet-4-5","permission":"CAN_QUERY"}},
  {"name":"embeddings","serving_endpoint":{"name":"databricks-gte-large-en","permission":"CAN_QUERY"}}]}'

# 4.3 Unity Catalog read access for the glossary and Genie tables.
#   GRANT USE CATALOG ON CATALOG <CATALOG> TO `<SP_CLIENT_ID>`;
#   GRANT USE SCHEMA, SELECT ON SCHEMA <CATALOG>.<SCHEMA> TO `<SP_CLIENT_ID>`;

# 4.4 Upload the app source (with app.yaml env set) and deploy.
databricks workspace import-dir v2/app /Workspace/Users/<your-email>/apps/fis-v2 --overwrite --profile <PROFILE>
databricks apps deploy fis-v2 --source-code-path /Workspace/Users/<your-email>/apps/fis-v2 --profile <PROFILE>
```

Grant that service principal a Lakebase role, then table access:

```bash
databricks postgres create-role projects/fis/branches/production \
  --role-id fis-v2-sp \
  --json '{"spec": {"postgres_role": "<SP_CLIENT_ID>", "identity_type": "SERVICE_PRINCIPAL"}}' \
  --profile <PROFILE>
```

```sql
GRANT USAGE ON SCHEMA public TO "<SP_CLIENT_ID>";
GRANT SELECT ON fis_tasks   TO "<SP_CLIENT_ID>";
```

> **Important:** Register the role via `w.postgres.create_role()` (SDK) or the CLI —
> **not** SQL `CREATE ROLE`. Lakebase OAuth requires roles registered through the
> control plane.

The app's service principal also needs read access to the `glossary` UC table (loaded
at startup) and query access to the Genie space.

**GATE 4** — the app is running and can reach Lakebase:

```bash
databricks apps get fis-v2 --profile <PROFILE> | grep -E '"state"|"url"'
```

Must be `RUNNING`. Then open the URL in a browser (an unauthenticated `curl` gets an
SSO redirect, which is expected — a human must confirm the chat renders) and ask a
retrieval question; a `password authentication failed for user '<UUID>'` in the app
logs means the SP is missing its Lakebase role (redo the grant above).

---

## Stage 5 — Report the run

Produce a table of every gate with its **observed** result, and state plainly what you
could **not** verify. At minimum, note that:

- Stage 2's build+load is currently manual (unscripted) — say exactly how you created
  and loaded `fis_tasks`, and the final row/embedding counts.
- The app UI needs a human — `curl` gets a 302 SSO redirect, so a person with a browser
  session must confirm the chat renders and returns cited task numbers.

---

## Failure playbook

| Symptom | Cause | Fix |
|---|---|---|
| `password authentication failed for user '<UUID>'` | App SP missing its Lakebase role | Create the role via SDK/CLI (Stage 4), then `GRANT` |
| `the query has N placeholders but M parameters were passed` | SQL parameter mismatch in hybrid search with filters | Check `text_where` / `all_params` construction in `fis_knowledge_search` |
| `relation "fis_tasks" does not exist` | Stage 2 table never created | Run the DDL in Stage 2 |
| Vector search returns nothing / errors on `<=>` | `lakebase_vector` unpopulated or index missing | Confirm GATE 2 counts and `idx_fis_tasks_vector` |
| `Genie query timed out` | Genie space warehouse idle | Retry — the warehouse auto-starts |
| `Glossary is not available` | App couldn't query the glossary table at startup | Check SQL warehouse access + SP permission on the UC `glossary` table |
| Agent answers without citing task numbers | Retriever returned nothing, or system prompt drift | Verify Stage 3 Suite 4/5; check `SYSTEM_PROMPT` in the notebook/app agree |

## Teardown

```bash
# Remove the app.
databricks apps delete fis-v2 --profile <PROFILE>

# Remove the Lakebase role (control-plane registered).
databricks postgres delete-role projects/fis/branches/production --role-id fis-v2-sp --profile <PROFILE>

# Drop the Lakebase table (via psql/psycopg).
#   DROP TABLE IF EXISTS fis_tasks;

# Remove the data-pipeline bundle resources.
databricks bundle destroy -t <TARGET> --auto-approve
```

Report anything that survives: the Lakebase `fis` project itself, the Genie space if it
was created outside the bundle, and any UC tables created by the pipeline jobs.
