# DEPLOYMENT — agent runbook

> **Audience: an AI agent deploying this template unattended.** Follow the stages in
> order. Every stage has a **GATE** you must pass before continuing. If a gate
> fails, stop and report — do not proceed to the next stage, and do not paper over a
> failure by re-running blindly.
>
> **Rules for this runbook**
> 1. Never report success you have not observed. Paste the actual command output.
> 2. A `--verify` that prints `VERIFY PASSED` is evidence. Your own reasoning is not.
> 3. If a step is skipped, say which one and why.
> 4. Long steps (KA indexing ~25 min) must be polled, not assumed. Auth can expire
>    mid-poll — see the auth note in Stage 0.

---

## Overview — what you deploy

The deploy is **phased**, not a single `bundle deploy`, because two native resources have
deploy-time dependencies on outputs that only exist after a job runs: the **Genie space**
validates that its backing table (`rd_tasks_serving_analytics`) exists (built by
`rkb_data_pipeline`), and the **front-door app** binds the **MAS serving endpoint** by name
(created by `rkb_agents`). So the order is: data infra (Stage 1) → run the pipeline
(Stage 2) → deploy Genie + the agents job and run it (Stages 2–3) → deploy the app bound to
the endpoint and start it (Stage 4). A single `bundle deploy` fails on a cold environment.

**Requirements.** Databricks CLI **v1.3.0+** and a **serverless** SQL warehouse (AI
Functions are not available on SQL Warehouse Classic).

**Common vars.** Every `bundle deploy`/`bundle run` below takes the same `--var` set; the
stages spell it out in full, but the shorthand is:

```bash
# Defaults: catalog=main, schema=troubleshooting_knowledge_agent
--var catalog=<CATALOG> --var schema=<SCHEMA> \
--var app_name=<APP_NAME> --var warehouse_id=<WAREHOUSE_ID>
```

This bundle uses **no `mode: development`** name prefixing — it deploys to exactly
`${var.catalog}.${var.schema}`. Isolate a personal deploy with a distinct `--var schema=`
(and `--var app_name=` when sharing a workspace), not via an automatic prefix.

**What exists after a full run:**

| Resource | What it is |
|---|---|
| **Data** (`<catalog>.<schema>`) | ticket corpus, silver, gold enrichment, and `rd_tasks_serving` (+ `rd_tasks_serving_analytics`) — the one table both engines read |
| **Knowledge Assistant** | indexed over the serving table's content column, two sources (corpus + glossary), citing via the metadata struct |
| **Genie space** | a native DAB `genie_spaces` resource over `rd_tasks_serving_analytics` (deployed in Stage 2 once the view exists, not script-built) |
| **Supervisor** | routes KA + Genie (by injected space id) + the `glossary_lookup` UC function |
| **App** | front-door chat (OBO to the Supervisor) |

**Idempotency.** Every stage is re-runnable. The pipeline is gated on a content hash, so a
re-run with no new tickets does **zero** LLM work. The agent build reuses assets by display
name, patches instructions, and attaches sources/examples only when absent.

---

## Stage 0 — Preconditions

```bash
# 0.1 Authenticate. Interactive: a human must run this if it fails.
databricks auth login --profile <PROFILE>

# 0.2 Prove the token works AND resolves to the intended workspace.
databricks auth env --profile <PROFILE> | head -5
databricks current-user me --profile <PROFILE>

# 0.3 CLI version — genie_spaces and the app resource bindings need a recent CLI.
databricks --version          # need v1.3.0+

# 0.4 Confirm a SERVERLESS SQL warehouse exists and is the one you will pass.
#     AI Functions do NOT run on SQL Warehouse Classic.
databricks warehouses get <WAREHOUSE_ID> --profile <PROFILE> | grep -E '"name"|"enable_serverless_compute"|"state"'
```

**GATE 0** — all four succeed, and `enable_serverless_compute` is `true`.

> **Auth model.** The build scripts authenticate via the Databricks SDK
> (`databricks-sdk`, added to the jobs' `environments.dependencies`): **ambient**
> credentials when they run as serverless job tasks, and the **`--profile`** you pass
> when you run them locally (e.g. the `--verify` commands below). The CLI profile is a
> *local* convenience only — serverless job compute has no usable named profile, so
> nothing here shells out to `databricks --profile` for workspace calls.

> **Auth expiry is a real failure mode.** A previous unattended run had its OAuth
> refresh token expire ~137s into a 25-minute KA poll. The poller reported
> `KA=UNKNOWN sources={}` for 8 minutes, which looked like a broken KA but was
> blind auth errors. If you ever see `UNKNOWN` or an empty source map, check
> `databricks auth token --profile <PROFILE>` **before** concluding anything about
> the KA.

---

## Stage 1 — Deploy the data infra (phased deploy, part 1 of 3)

> **The deploy is PHASED, not one `bundle deploy`.** Two native resources have
> deploy-time dependencies on job outputs: the **Genie space** validates that its
> backing table (`rd_tasks_serving_analytics`) exists (built by `rkb_data_pipeline`),
> and the **app** binds the **MAS endpoint** by name (created by `rkb_agents`). So the
> order is: infra (Stage 1) → run pipeline + deploy Genie/agents (Stage 2) → run agents
> (Stage 3) → deploy + start the app bound to the endpoint (Stage 4). A single
> `bundle deploy` fails on a cold environment.
>
> **No name prefixing.** This bundle has no `mode: development`; the deploy targets
> exactly `${var.catalog}.${var.schema}`. Isolate a personal deploy with a distinct
> `--var schema=` (and `--var app_name=`), not an automatic prefix.

```bash
# Render the Genie space payload for your catalog/schema (DAB does not interpolate
# inside the genie JSON). Writes git-ignored genie/genie_space.json from the template.
python3 src/deploy/render_genie.py --catalog <CATALOG> --schema <SCHEMA>

databricks bundle validate -t <TARGET>

# Inspect the plan BEFORE mutating anything.
# `bundle plan` is its own subcommand. `bundle deploy` has NO dry-run: its --plan
# flag takes a path to a JSON plan file, it is not a preview switch.
databricks bundle plan -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID>
```

Output is a create/change/delete list, e.g.

```
create apps.frontdoor
create genie_spaces.rkb_serving
...
Plan: 9 to add, 0 to change, 0 to delete, 0 unchanged
```

**STOP AND READ IT.** Anything under `create` that you know **already exists** in
the workspace will become a **duplicate**, not an adoption — a bundle adopts an
existing resource only when the name matches exactly. This is the single most
likely way to make a mess here, because the apps and the Genie space may already
have been created by the scripts directly.

If you see a `create` for something that exists: stop and either reconcile the
names or `databricks bundle deployment bind <resource_key> <existing_id> -t <TARGET>`.
Report what you found before continuing.

Also read the `delete` lines. On a re-deploy, an unexpected `delete` means the
bundle is about to remove something you want to keep.

```bash
# Phase 1 — deploy ONLY the data infra (--select). Genie + agents deploy in Stage 2
# (after the table exists); the app deploys in Stage 4 (after the endpoint exists).
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --var app_name=<APP_NAME> \
  --select schemas.rkb --select volumes.glossary \
  --select jobs.rkb_data_pipeline
```

**GATE 1**

```bash
databricks bundle summary -t <TARGET>
```

Expect the 3 infra resources: `schemas.rkb`, `volumes.glossary`, and job
`rkb_data_pipeline`. (The full bundle is 7 resources + the volume grant;
`genie_spaces.rkb_serving` + `jobs.rkb_agents` arrive in Stage 2 and
`apps.frontdoor` + `jobs.rkb_frontdoor_authz` in Stage 4.) Missing infra → stop.

---

## Stage 2 — Data pipeline

```bash
databricks bundle run rkb_data_pipeline -t <TARGET>

# Phase 2 deploy — the analytics view now exists, so the Genie space validates, and the
# agents job can resolve ${resources.genie_spaces.rkb_serving.id}.
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --var app_name=<APP_NAME> \
  --select genie_spaces.rkb_serving --select jobs.rkb_agents
```

Tasks in order: `parse_tickets` → `load_tables` → {`build_silver`, `glossary`} →
`enrich` → `serving`. Bronze (`parse_tickets`/`load_tables`), the
silver layer (`data_generation/build_silver.py` → `rd_tasks_silver` +
`rd_task_note_entries`), and the SME-governed `glossary` are `spark_python_task`s;
`glossary` mines the vocabulary from bronze `rnd_tickets` so it is available before
enrichment. **`enrich` and `serving` are serverless `notebook_task` notebooks**
(`src/notebooks/enrich.py`, `src/notebooks/serving.py`). `enrich` builds
`rd_tasks_gold_enrichment` with `ai_query`, **incremental** via a `content_hash` LEFT
ANTI JOIN + `MERGE`: it calls the LLM only on new/changed tickets, so a first run budgets
~10 min and a re-run with no new tickets does **zero `ai_query` work** (0 `todo` rows →
0 model calls → MERGE skipped). `serving` then builds `rd_tasks_serving` as a **plain
Delta table** (CDF on) — silver ⋈ `rnd_tickets` ⋈ enrichment ⋈ note counts, with
`ka_content` composed — so the KA can *stream* from it (a materialized view cannot be
streamed), then creates the Genie analytics views (`rd_tasks_serving_analytics` and the
compat alias `rd_tasks_gold_analytics`) and runs `verify()`. A changed ticket re-enriches
on the next run because its `content_hash` changes (the anti-join picks it up and the
`MERGE` updates it by `number`).

Poll the run (`databricks jobs get-run`, not the CLI stream) and pull failed-task output
with `jobs get-run-output`. The `serving` notebook's `verify()` gates data quality
(`ka_content` non-empty, `metadata.file_path` present, enrichment populated, 1:1 grain,
**and that `rd_tasks_serving` is a plain table, not a view/MV**); GLO-02 is enforced at
generation because the `ai_query` enum is built from the approved glossary at run time.

**GATE 2 — the serving table must satisfy BOTH engines.**

```bash
python3 src/deploy/build_serving_table.py --profile <PROFILE> --verify \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>
```

Requires the line `VERIFY PASSED`. (The `serving` notebook already runs this same
`verify()` in-job at the end of Stage 2; this local re-run is a convenience.) The checks
that block Stage 3:

| Check | Why it blocks |
|---|---|
| `is a TABLE, not a view/MV` | KA sync streams from the table; streaming from an MV fails with `STREAMING_FROM_MATERIALIZED_VIEW` (CDF alone does not make an MV streamable) |
| `metadata struct present` | KA attach fails with `missing required column '_metadata'` |
| `CDF enabled` | KA attach fails: *"must either be a streaming table or have Change Data Feed enabled"* |
| `ka_content present` | `file_col` needs exactly one pre-composed column |

Also confirm the glossary actually has approved terms — enrichment enums are built
from them at run time, and an empty glossary means an uncontrolled vocabulary:

```bash
python3 src/deploy/build_glossary.py --profile <PROFILE> --verify-only \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>
```

**GATE 2b — GLO-02 coupling.** The `enrich` notebook builds the `ai_query`
`systems_involved`/`vendors` enums from `glossary WHERE status='approved'` at run time
(`enrich_recipe.parse_vocab`), so the enum cannot contain a term the glossary has not
approved — the coupling holds by construction, not by a separate check. To spot-check that
no emitted system value is outside the approved set, run the two-way EXCEPT directly:

```sql
(SELECT DISTINCT explode(systems_involved) FROM <cat>.<schema>.rd_tasks_gold_enrichment)
EXCEPT (SELECT term FROM <cat>.<schema>.glossary WHERE status='approved' AND category='system')
```

An empty result means no drift. (A non-empty *reverse* direction — approved terms unused
by any ticket — is fine, not a failure.)

---

## Stage 3 — Agents (the long one)

```bash
databricks bundle run rkb_agents -t <TARGET>
```

Two tasks: `serving_agents` (KA + Genie) then `supervisor`.

**Expect ~25 minutes.** A measured run reached `ACTIVE` with both sources
`UPDATED` at **1434s**. Poll; do not assume. Healthy intermediate output looks like:

```
[KA] t+400s KA=CREATING sources={'rd_tasks_serving_corpus': 'UPDATING', 'rkb_glossary': 'UPDATING'}
```

`CREATING`/`UPDATING` with no error is **normal**, not stuck. What is *not* normal:

| Symptom | Meaning | Action |
|---|---|---|
| `KA=UNKNOWN sources={}` | blind auth errors, not a KA problem | re-auth (Stage 0), re-run with `--verify` |
| `FAILED_UPDATE` on a source | real indexing failure | stop, report the source and its state |
| glossary source `NOT_FOUND` | `files.path` was given a FILE | must be the **directory** path |

**GATE 3**

```bash
python3 src/deploy/build_serving_agents.py --profile <PROFILE> --verify \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>
```

Requires **11/11 PASS**. Two checks deserve attention because they catch a silent
quality regression rather than an outage:

- **`instructions MATCH the live KA`** — the KA's instructions are what produce
  cited, actionable answers. Measured on byte-identical indexed content: the tuned
  four-rule instructions give **6.0** avg citations; a vaguer paragraph form gives
  **1.2**, with 0/5 answers carrying a `Sources:` line. A KA that returns polite,
  uncited prose is almost always an instructions problem, not retrieval.
- **`examples attached == 8`** — the instructions end with *"See the labeled
  Examples"*. Zero examples points the model at guidance that does not exist.

---

## Stage 4 — Front-door app

```bash
# Phase 3 deploy — bind the app to the MAS endpoint rkb_agents created in Stage 3.
# <ENDPOINT> is the serving-endpoint name that job reported.
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --var app_name=<APP_NAME> --var mas_endpoint_name=<ENDPOINT> \
  --select apps.frontdoor --select jobs.rkb_frontdoor_authz

# OBO scopes + the serving-endpoint resource binding — the DAB App resource cannot
# express user_api_scopes, so frontdoor_deploy.py owns it (bound to --var mas_endpoint_name).
databricks bundle run rkb_frontdoor_authz -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --var app_name=<APP_NAME> --var mas_endpoint_name=<ENDPOINT>

# The app needs an explicit run to start.
databricks bundle run frontdoor -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID> \
  --var app_name=<APP_NAME>
```

**GATE 4**

```bash
databricks apps get <APP_NAME> --profile <PROFILE> | grep -E '"state"|"url"'
```

Must be `RUNNING`. Then confirm the OBO wiring actually landed — this is the
step DAB cannot do, so it is the step most likely to be silently missing:

```bash
databricks apps get <APP_NAME> --profile <PROFILE> \
  | grep -A3 -E 'user_api_scopes|resources'
```

Expect `serving.serving-endpoints` in the scopes and the MAS endpoint bound
`CAN_QUERY`.

> **`curl` on the app URL returns 302, and that is CORRECT.** These are OBO apps;
> an unauthenticated request is supposed to redirect to SSO. Do **not** report 302
> as a failure. It also means you cannot verify the UI yourself — see Stage 6.

---

## Stage 5 — Functional tests

Run all three. These query the live agents, so they cost tokens and take minutes.

```bash
python3 src/deploy/test_ka.py         --profile <PROFILE>   # retrieval + citations
python3 src/deploy/test_genie.py      --profile <PROFILE>   # NL->SQL correctness
python3 src/deploy/test_supervisor.py --profile <PROFILE>   # routing across archetypes
```

**GATE 5** — all three report pass. `test_genie.py` is the one that catches the
`array_contains` vs `ILIKE` category bug; `test_supervisor.py` catches
silent-redirect routing failures.

### Optional: scored evaluation

```bash
python3 eval/run_eval.py --profile <PROFILE>
```

Scores correctness, relevance, citation-groundedness and plausible-reasoning via
MLflow. Run this if the KA's indexed content or instructions changed — prior scores
describe the prior configuration and stop being valid.

---

## Stage 6 — Report the run

Produce a table of every gate with its **observed** result, and state plainly what you
could **not** verify. At minimum, the app UI needs a human: `curl` gets a 302 SSO
redirect, so a person with a browser session must confirm the chat renders, the progress
line ticks through its stages, the citation chips are clickable, and the four demo acts in
`README.md` all work. Note anything you skipped, with the reason.

---

## Failure playbook

| Symptom | Cause | Fix |
|---|---|---|
| `missing required column '_metadata'` | KA source table has no metadata struct | rebuild the serving table (Stage 2) |
| `must either be a streaming table or have CDF enabled` | pointed the KA at a **view**, or CDF off | KA sources must be physical tables with CDF |
| `Array must have size 1, but has size 2` | passed two columns to `file_col` | pre-compose one content column |
| `Duplicate knowledge source paths` | that table is already attached | reuse the existing source; do not re-attach |
| Glossary source `NOT_FOUND` | gave `files.path` a file | use the **directory** |
| Answers uncited / no `Sources:` line | instruction drift | Gate 3 instruction-parity check |
| Answer appears 2-3x with `<name>` tags | rendering the whole routing trace | render the final turn only |
| Genie returns 0 for a known term | non-`system` term hit `array_contains` | resolve category first, use `ILIKE` |
| App 504 on a hard question | held one request past the 120s proxy limit | submit/poll, never one blocking call |
| `KA=UNKNOWN`, empty source map | expired auth mid-poll | re-auth, re-run `--verify` |

## Teardown

```bash
databricks bundle destroy -t <TARGET> --auto-approve
```

**Incomplete by design.** This does **not** remove the Knowledge Assistant or the
Supervisor — DAB has no resource type for them, so they are script-created and must
be deleted via the Agent Bricks UI or API. UC tables created by the jobs also
survive. Report both as remaining.
