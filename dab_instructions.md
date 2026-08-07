# Deploy — R&D Troubleshooting Knowledge Assistant

Four steps. The agent build is separate from the pipeline because the Knowledge
Assistant cannot attach to its source table until that table exists with Change
Data Feed enabled and a `metadata` struct.

```bash
# 1. UC schema + volume, Genie space, both apps, job definitions
databricks bundle deploy \
  --var catalog=dbdemos_templates \
  --var schema=rnd_knowledge_agent \
  --var warehouse_id=<your-serverless-warehouse-id>

# 2. Data: parse -> silver -> glossary -> LLM enrichment -> serving table
databricks bundle run fis_data_pipeline

# 3. Agents: Knowledge Assistant, Genie config, Supervisor
#    Expect ~25 min — the KA indexes the corpus and the job polls to ACTIVE.
databricks bundle run fis_agents

# 4. Front door: bind the OBO scopes DAB cannot express, then start the app
databricks bundle run fis_frontdoor_authz
databricks bundle run frontdoor
```

Requires Databricks CLI **v1.3.0+** and a serverless SQL warehouse (AI Functions
are not available on SQL Warehouse Classic).

After the run:
- **Data** — `<catalog>.<schema>`: ticket corpus, silver, gold enrichment, and
  `rd_tasks_serving` (+ `rd_tasks_serving_analytics`) — the one table both engines
  read.
- **Knowledge Assistant** — indexed over the serving table's content column, two
  sources (corpus + glossary), citing via the metadata struct.
- **Genie space** — over `rd_tasks_serving_analytics`.
- **Supervisor** — routes KA + Genie + the `glossary_lookup` UC function.
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
