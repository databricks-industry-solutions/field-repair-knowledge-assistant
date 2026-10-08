# FIS v2 — Deployment Guide

## Prerequisites

- Databricks workspace with Unity Catalog enabled
- Access to `serverless_stable_l26d62_catalog.fis_knowledge_agent` schema
- Lakebase project `fis` with `production` branch and `primary` endpoint
- Serving endpoints: `databricks-gte-large-en` (embedding), `databricks-claude-sonnet-4-5` (LLM)
- Genie space `01f190db953e1140b39d10f49a46aa7b` configured

## Step 1 — Lakebase Setup

### 1.1 Verify Lakebase Project

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
ep = w.postgres.get_endpoint(name="projects/fis/branches/production/endpoints/primary")
print(ep.status.hosts.host)  # Should show the endpoint host
```

### 1.2 Create Table & Indexes

Run the bundle's data job (`rkb_data_pipeline`). Its `lakebase_sync` task
(`lakebase/lakebase_sync.py`) reads the Lakeflow silver table
`servicenow_rd_task_silver` plus enrichment, embeds new or changed tickets, and
upserts them into Lakebase:

```bash
databricks bundle deploy -p <profile> --var warehouse_id=<id>
databricks bundle run rkb_data_pipeline -p <profile> --var warehouse_id=<id>
```

The task creates (idempotently):
- `fis_tasks` table with 27 columns including `lakebase_vector` (1024-dim) and `lakebase_text`
- Primary key on `number` (upsert target) and `content_hash` / `synced_at` columns
- IVFFlat vector index (`idx_fis_tasks_vector`), created once the table has rows
- GIN full-text index (`idx_fis_tasks_text`)

### 1.3 Create Lakebase Role for App SP

After creating the app (Step 3), the app's service principal needs a Lakebase role:

```bash
databricks postgres create-role projects/fis/branches/production \
  --role-id fis-v2-sp \
  --json '{"spec": {"postgres_role": "<SP_CLIENT_ID>", "identity_type": "SERVICE_PRINCIPAL"}}'
```

Then grant access:
```sql
GRANT USAGE ON SCHEMA public TO "<SP_CLIENT_ID>";
GRANT SELECT ON fis_tasks TO "<SP_CLIENT_ID>";
```

> **Important:** Use `w.postgres.create_role()` (SDK) or the CLI — NOT SQL `CREATE ROLE`. Lakebase OAuth requires roles registered through the control plane.

## Step 2 — Test the Agent

Run the test notebook (`v2/tests/fis_v2_tests.py`) end-to-end:

```
8 test suites, 35+ tests:
  Suite 1: Lakebase Connectivity (6 tests)
  Suite 2: Embedding Model (4 tests)
  Suite 3: Vector Search (4 tests)
  Suite 4: Retriever Tool (4 tests)
  Suite 5: LangGraph Agent — 3 tools (4 tests)
  Suite 6: Edge Cases & Data Quality (5 tests)
  Suite 7: Glossary Lookup (8 tests)
  Suite 8: Genie Query (3 tests)
```

All tests must pass before deploying the app.

## Step 3 — Deploy the App

### 3.1 Create the App

```bash
databricks apps create fis-v2
```

### 3.2 Deploy

```bash
databricks apps deploy fis-v2 \
  --source-code-path /Workspace/Users/<your-email>/field-repair-knowledge-assistant/v2/app
```

Or via Python SDK:
```python
from databricks.sdk.service.apps import AppDeployment, AppDeploymentMode
w.apps.deploy(
    app_name="fis-v2",
    app_deployment=AppDeployment(
        source_code_path="/Workspace/Users/<your-email>/field-repair-knowledge-assistant/v2/app",
        mode=AppDeploymentMode.SNAPSHOT,
    )
).result()
```

### 3.3 Post-Deploy — Grant Lakebase Access

After the first deploy, the app's SP UUID is available:
```bash
databricks apps get fis-v2 --output JSON | jq .service_principal_client_id
```

Use that UUID in Step 1.3 to create the Lakebase role and grants.

## Configuration

| Parameter | Value | Location |
| --- | --- | --- |
| `LAKEBASE_HOST` | `ep-long-feather-d20bt18w.database.us-east-1.cloud.databricks.com` | app.py, agent notebook |
| `LAKEBASE_DB` | `databricks_postgres` | app.py, agent notebook |
| `LAKEBASE_TABLE` | `fis_tasks` | app.py, agent notebook |
| `EMBEDDING_MODEL` | `databricks-gte-large-en` | app.py, agent notebook |
| `LLM_ENDPOINT` | `databricks-claude-sonnet-4-5` | app.py, agent notebook |
| `GLOSSARY_TABLE` | `serverless_stable_l26d62_catalog.fis_knowledge_agent.glossary` | app.py, agent notebook |
| `GENIE_SPACE_ID` | `01f190db953e1140b39d10f49a46aa7b` | app.py, agent notebook |
| `TOP_K` | `5` | app.py, agent notebook |

## Troubleshooting

| Error | Cause | Fix |
| --- | --- | --- |
| `password authentication failed for user '<UUID>'` | App SP missing Lakebase role | Create role via SDK/CLI (Step 1.3) |
| `the query has N placeholders but M parameters were passed` | SQL parameter mismatch in hybrid search with filters | Verify `text_where` and `all_params` construction in `fis_knowledge_search` |
| `Genie query timed out` | Genie space SQL warehouse is idle | Retry — the warehouse auto-starts |
| `Glossary is not available` | App couldn't query the glossary table at startup | Check SQL warehouse access and SP permissions on the UC table |
