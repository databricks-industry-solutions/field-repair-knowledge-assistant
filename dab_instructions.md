# Deploy — R&D Troubleshooting Knowledge Assistant

The deploy is **phased**, not a single `bundle deploy`, because two native resources
have deploy-time dependencies on outputs that only exist after a job runs:

- the **Genie space** validates that its backing table (`rd_tasks_serving_analytics`)
  exists — but that view is built by `fis_data_pipeline`;
- the **front-door app** binds the **MAS serving endpoint** by name — but that endpoint
  is created by `fis_agents`.

So: deploy the data infra → run the pipeline → deploy Genie + the agents job → run the
agents → deploy the app bound to the endpoint → start it. Every command below takes the
same `--var` set (shown once); set your own `catalog`/`schema`/`app_name` to isolate.

```bash
# Common vars (repeat on every deploy/run). Defaults: main / troubleshooting_knowledge_agent.
#   --var catalog=<your-catalog> --var schema=<your-schema>
#   --var app_name=<your-app-name> --var warehouse_id=<your-serverless-warehouse-id>

# 0. Render the Genie space payload for your catalog/schema. DAB does NOT interpolate
#    inside the genie JSON, so this writes genie/genie_space.json (git-ignored) from
#    genie/genie_space.template.json. Re-run whenever catalog/schema changes.
python3 src/deploy/render_genie.py --catalog <your-catalog> --schema <your-schema>

# 1. Deploy the data infra only (schema, volume, data job).
databricks bundle deploy <vars> \
  --select schemas.fis --select volumes.glossary \
  --select jobs.fis_data_pipeline

# 2. Data: parse/load (bronze) -> build_silver -> glossary -> enrich (notebook) ->
#    serving (notebook: plain Delta rd_tasks_serving + analytics views + verify).
databricks bundle run fis_data_pipeline <vars>

# 3. Now the table exists — deploy the Genie space (native) + the agents job (which
#    is injected with the Genie space id).
databricks bundle deploy <vars> \
  --select genie_spaces.fis_rnd_serving --select jobs.fis_agents

# 4. Agents: Knowledge Assistant + Supervisor. ~25 min (KA indexes, job polls ACTIVE).
#    NOTE the MAS serving-endpoint name it reports — you pass it in step 5.
databricks bundle run fis_agents <vars>

# 5. Deploy the app + authz job bound to that endpoint, bind OBO scopes, then start.
databricks bundle deploy <vars> --var mas_endpoint_name=<endpoint-from-step-4> \
  --select apps.frontdoor --select jobs.fis_frontdoor_authz
databricks bundle run fis_frontdoor_authz <vars> --var mas_endpoint_name=<endpoint-from-step-4>
databricks bundle run frontdoor <vars>
```

Requires Databricks CLI **v1.3.0+** and a serverless SQL warehouse (AI Functions
are not available on SQL Warehouse Classic). This bundle uses **no `mode: development`
name prefixing** — isolate a personal deploy by passing a distinct `--var schema=` (and
`--var app_name=` when sharing a workspace), not via an automatic prefix.

After the run:
- **Data** — `<catalog>.<schema>`: ticket corpus, silver, gold enrichment, and
  `rd_tasks_serving` (+ `rd_tasks_serving_analytics`) — the one table both engines
  read.
- **Knowledge Assistant** — indexed over the serving table's content column, two
  sources (corpus + glossary), citing via the metadata struct.
- **Genie space** — a native DAB `genie_spaces` resource over
  `rd_tasks_serving_analytics` (deployed in step 3 once the view exists, not script-built).
- **Supervisor** — routes KA + Genie (by injected space id) + the `glossary_lookup` UC function.
- **App** — front-door chat (OBO to the Supervisor).

## Idempotency

Every step is re-runnable. The pipeline is gated on a content hash, so a re-run
with no new tickets does **zero** LLM work. The agent build reuses assets by
display name, patches instructions, and attaches sources/examples only when
absent.

## Verification

```bash
# 11 assertions on the serving table (metadata struct, CDF, content parity, grain)
python3 src/deploy/build_serving_table.py --verify

# 11 assertions on the agents (state, source wiring, instruction + example parity)
python3 src/deploy/build_serving_agents.py --verify
```

## Teardown

```bash
databricks bundle destroy --auto-approve
```

**This does not remove the Knowledge Assistant or the Supervisor.** DAB has no
resource type for them, so they are created by scripts and must be deleted from
the Agent Bricks UI or via the API. UC tables created by the jobs also survive.
