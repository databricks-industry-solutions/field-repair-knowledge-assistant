# Deployment Validation — Session Handoff

> Working notes for resuming the end-to-end deployment validation of this solution
> template. Delete this file once validation is complete. It is NOT part of the template.

## Goal

Validate that this Databricks solution template **actually deploys and runs end-to-end**
in the workspace `fevm-serverless-stable-l26d62`, and fix the template so it does.

Two objectives:
1. **Validate the deployment** — deploy the DAB bundle and run every stage on serverless,
   observing each gate.
2. **Validate the documentation** — every file is used; the deploy/verify steps in the
   docs are correct.

## Overarching finding

The template was only ever run **locally via the CLI**, never through its own DAB
**serverless job tasks**. Every layer had a serverless-incompatibility. We found and fixed
**12+ systemic blockers** and validated the data + Genie + KA layers live.

## Deployment coordinates (use these exact values to resume)

- **Profile:** `serverless-stable`  (host `https://fevm-serverless-stable-l26d62.cloud.databricks.com`)
  - If auth expired: `databricks auth login --profile serverless-stable` (interactive).
- **Target:** `dev`  (no `mode:` → NO name prefixing; isolate via `--var`)
- **Vars (pass on every deploy/run):**
  ```
  --var catalog=serverless_stable_l26d62_catalog
  --var schema=fis_knowledge_agent_dev
  --var app_name=fis-rnd-frontdoor-dev
  --var agent_suffix=-dev
  --var warehouse_id=04a4dee7888b9e64
  ```
- **Warehouse:** `04a4dee7888b9e64` (Serverless Starter Warehouse; serverless=true).

## Status by stage

| Stage | What | Status |
|---|---|---|
| 0 | Auth + serverless warehouse | ✅ done |
| 1 | Deploy infra (schema/volume/pipeline/data job) | ✅ done |
| 2 | Run `fis_data_pipeline`; **GATE 2** serving-table verify | ✅ **PASSED** (23 tickets, CDF, metadata, ka_content, acronym expansion, analytics view) |
| 2b | Deploy Genie space + agents job (phased) | ✅ done (dev Genie space `01f19b2c…`) |
| 3 | Run `fis_agents` (KA + MAS) | ✅ **PASSED** (GATE 3). Blockers #15/#16/#17 fixed. Job run `677357740766336`: both tasks SUCCESS on serverless, supervisor smoke PASS. MAS `fis-rnd-supervisor-dev` = `b5da5fdd-cdd5-4de7-85e8-d9c252b75fe8`, endpoint **`mas-b5da5fdd-endpoint`**. |
| 4 | App (frontdoor OBO + endpoint) | ⛔ not started (frontdoor_deploy rewritten, ready) |
| 5 | Functional tests (test_ka/genie/supervisor) | ⛔ not started |
| 6 | Cleanup dev state | ⛔ pending (user approved destroying dev state when done) |

## RESUME HERE

### 1. Check the in-flight agents run (Stage 3)
```bash
databricks jobs get-run 597529122807899 --profile serverless-stable -o json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);s=d['state'];print(s.get('life_cycle_state'),s.get('result_state'));[print(' ',t['task_key'],t['state'].get('result_state')) for t in d['tasks']]"
```
- If a task FAILED, get the real error (CLI stream hides it):
  ```bash
  # get the task run_id from get-run above, then:
  databricks jobs get-run-output <TASK_RUN_ID> --profile serverless-stable -o json \
    | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('error','')[:400]);print((d.get('logs') or '')[-2000:])"
  ```
- If it SUCCEEDED → **GATE 3**:
  ```bash
  python3 src/deploy/build_serving_agents.py --profile serverless-stable --verify \
    --catalog serverless_stable_l26d62_catalog --schema fis_knowledge_agent_dev \
    --warehouse-id 04a4dee7888b9e64 --agent-suffix=-dev
  ```
  Expect the KA-`-dev` checks to pass (KA corpus points at `…fis_knowledge_agent_dev.rd_tasks_serving`).
  Note the MAS **serving endpoint name** the `supervisor` task printed — needed for Stage 4.

### 2. Stage 4 — front-door app (phased deploy, part 3)
```bash
MAS_ENDPOINT=<endpoint the supervisor task reported>
databricks bundle deploy -t dev --profile serverless-stable \
  --var catalog=serverless_stable_l26d62_catalog --var schema=fis_knowledge_agent_dev \
  --var app_name=fis-rnd-frontdoor-dev --var agent_suffix=-dev \
  --var warehouse_id=04a4dee7888b9e64 --var mas_endpoint_name=$MAS_ENDPOINT \
  --select apps.frontdoor --select jobs.fis_frontdoor_authz
databricks bundle run fis_frontdoor_authz -t dev --profile serverless-stable \
  --var ... (same vars incl. mas_endpoint_name)
databricks bundle run frontdoor -t dev --profile serverless-stable --var ... (same vars)
```
GATE 4: `databricks apps get fis-rnd-frontdoor-dev --profile serverless-stable` → RUNNING,
`user_api_scopes` includes `serving.serving-endpoints`, MAS bound CAN_QUERY.
(App UI itself is behind SSO 302 — a human must click through; agents can't verify it.)

### 3. Stage 5 — functional tests (run locally; SDK uses the profile)
```bash
python3 src/deploy/test_ka.py --profile serverless-stable
python3 src/deploy/test_genie.py --profile serverless-stable
python3 src/deploy/test_supervisor.py --profile serverless-stable
```
NOTE: these may still target hardcoded demo KA/MAS endpoints — check/verify they hit the
`-dev` agents (likely need the same discovery/suffix treatment; not yet audited).

### 4. Cleanup (user approved destroying the dev state when done)
```bash
databricks bundle destroy -t dev --profile serverless-stable --auto-approve --var ...(all vars)
```
Also: `fis-rnd-knowledge-assistant-serving-dev` KA + the `-dev` MAS are Agent-Bricks assets
NOT removed by destroy — delete via the Agent Bricks UI/API. The dev Genie space
`01f19b2c…` is destroyed by the bundle.

## What was fixed (all committed to the working tree; NOT git-committed)

Root causes → fixes:
1. **dev-mode name prefixing** desynced schema from job `--schema` → removed `mode:` from `dev`
   target (`presets.name_prefix:""` does NOT work); isolate via `--var`. (`databricks.yml`)
2. **Genie deploy-ordering** (native Genie needs its table first) → **phased deploy** +
   `src/deploy/render_genie.py` renders `genie/genie_space.template.json` →
   `genie/genie_space.json` (git-ignored) per catalog/schema. (`resources/genie.yml` file_path
   unchanged.)
3. **App↔MAS chicken-and-egg** → app deploys in phase 3 with `--var mas_endpoint_name`;
   `frontdoor_deploy.py` takes `--mas-endpoint-name`/`--app-name`; DAB owns app code+start.
4. **Supervisor Genie-by-title (ambiguous)** → inject `${resources.genie_spaces.fis_rnd_serving.id}`.
5. **Corpus hardcoded to a laptop path & not in repo** → staged to `data/servicenow/`
   (3 RnD_*.md; **Prompts.md deliberately NOT committed** — it's the private eval answer key,
   gitignored); `parse_tickets.py` resolves it relative to itself / `FIS_SAMPLE_DIR`.
6. **`__file__` undefined on serverless** → `Path.cwd()` fallback shim across 8 scripts
   (serverless execs the file with no `__file__`, CWD = the script's dir).
7. **`load_tables` missing target params + stale `from preflight.preflight import`** →
   parameterized (`--catalog/--schema/--warehouse-id` + `_apply_target`) + fixed import.
8. **Success `sys.exit(0)` flagged as task failure** → guarded to fail-only (`parse_tickets`,
   `frontdoor_deploy`).
9. **CORE: CLI-profile auth fails on serverless job compute** → re-plumbed to the **Databricks
   SDK `WorkspaceClient`** (ambient in-job, `--profile` locally). `preflight.py` gained
   `workspace_client()` + `api_do()` and rewrote `run_sql`/`first_warehouse_id`/
   `assert_target_host`/`resolve_principal`; `build_ka`/`build_supervisor`/`build_glossary`
   `run_cli` route `api`/`fs`/`auth env` via the SDK; `frontdoor_deploy` uses `w.apps`.
   Added `databricks-sdk` to all job `environments.dependencies`.
10. **Glossary had zero approved terms** (enrichment refused) → `build_glossary` auto-seeds the
    curated **authoritative** terms as `status='approved'` (Stage 5) — mined proposals stay for
    SME review.
11. **`glossary_lookup` UC function never created anywhere** → `build_glossary` now creates it
    (Stage 6).
12. **Agent identities hardcoded to shared-demo** (KA name, KA endpoint/tile, MAS name, glossary
    FQN) → `--agent-suffix` (var `agent_suffix`) suffixes KA + MAS names; `build_supervisor`
    **discovers** the KA endpoint by name (dropped hardcoded tile) + retargets `uc_function_name`
    to the deploy schema.
13. **`build_ka` glossary Volume paths not retargeted** → `build_ka._apply_target` recomputes
    them; `build_serving_agents._apply_target` calls `ka_mod._apply_target`.
14. Demo-specific `== 223` row assertion → soft/non-empty (`FIS_EXPECTED_ROWS` to pin).
15. **CORE (session 2): KA cannot stream from a materialized view.** The KA `file_table`
    sync does a *streaming* read; the SDP refactor built `rd_tasks_serving` as a
    **materialized view**, so the sync failed with `STREAMING_FROM_MATERIALIZED_VIEW`
    (CDF-on does NOT make an MV streamable). GATE 2 only checked "CDF enabled", so it
    passed and masked this. **Fix (user-approved architecture change): dropped SDP
    entirely.** Deleted `resources/enrich_pipeline.pipeline.yml` + `src/pipeline/`; rebuilt
    the enrichment + serving stages as **serverless notebook_task notebooks**
    `src/notebooks/enrich.py` (incremental `ai_query` via a `content_hash` LEFT ANTI JOIN +
    `MERGE`) and `src/notebooks/serving.py` (builds `rd_tasks_serving` as a **plain Delta
    table**, CDF on — streamable; then analytics views + `verify()`). Moved the shared
    `enrich_recipe.py` to `src/notebooks/`. Added a **table-TYPE check** (MANAGED/EXTERNAL,
    not VIEW/MV) to both `serving.py` verify() and `build_serving_agents.py::check_prereqs`
    — the gap that let the MV pass. `build_serving_table.py` demoted to a LOCAL verify/
    analytics CLI (no longer a job task). Docs updated (DEPLOYMENT/dab_instructions/
    architecture/specs). See the approved plan
    `~/.claude/plans/you-are-right-that-radiant-penguin.md`.
    NOTE: removing the pipeline left an orphaned `fis_enrich_pipeline` in the bundle's
    direct-backend state (`.databricks/bundle/dev/resources.json` + the workspace copy),
    which broke `bundle deploy` ("failed to compute relative path for pipeline"). Fixed by
    deleting the workspace pipeline and removing the `resources.pipelines.fis_enrich_pipeline`
    key from local state, then re-deploying.
16. **Supervisor grants/probe hit the wrong schema on retarget.** `build_supervisor.py`
    never defined a module-level `FQ`, so `_apply_target`'s `old_fq` was `None` and the
    FQN-rewrite loop no-op'd — `GLOSSARY_FN` / `ANALYTICS_VIEW` stayed on the default demo
    schema (`main.troubleshooting_knowledge_agent.*`), so the grants + SELECT probe
    false-failed on the `-dev` deploy (task exit 7). **Fix:** added
    `FQ = f"{CATALOG}.{SCHEMA}"` at module scope (mirrors `build_serving_agents.py` /
    `build_glossary.py`), so the existing rewrite retargets both FQNs.
17. **CORE: the MAS came up with zero callable tools ("Error: Tool 'X' not found").**
    `build_supervisor.py` sent an inline `agents[]` list on the create body; the **current**
    Supervisor Agent API ignores that. Tools are separate sub-resources:
    `POST /api/2.1/supervisor-agents/{id}/tools?tool_id=<name>` with `{tool_type,
    description, <type-block>}` (`knowledge_assistant.knowledge_assistant_id` /
    `genie_space.id` / `uc_function.name`); examples are `POST …/examples` with
    `{question, guidelines[]}`. The supervisor LLM saw the tool *names* but every call
    errored "not found" (task exit 8 on the smoke test). **Fix:** rewrote
    `create_or_update_mas` to create the MAS bare, then `reconcile_tools` (attach-if-missing,
    all 3, fatal if any fails) + `reconcile_examples` (wraps the legacy single `guideline`
    into `guidelines[]`) before polling; KA binds by its discovered **id** (`resolve_ka`
    returns id + endpoint). Wire shapes confirmed live via the `databricks
    supervisor-agents` CLI + `--debug` (tool_id is a query param). `--recreate` deletes +
    re-attaches to change a binding. Full local build now passes end-to-end (smoke PASS).

### Objective 2 (docs) — DONE earlier in the session
Fixed dangling refs to deleted modules, ghost `agents/` paths, architecture diagram + tree,
catalog/schema reconciliation, corpus location, **phased-deploy runbook** in `DEPLOYMENT.md`
+ `dab_instructions.md`, and the SDK auth-model note.

## Files changed (working tree, uncommitted)
- `databricks.yml` (dev target no-mode; `app_name` + `agent_suffix` vars)
- `resources/genie.yml` (unchanged path), `resources/jobs_pipeline.yml`, `resources/jobs_agents.yml`
  (target params, `--agent-suffix=`, `--mas-endpoint-name`, `databricks-sdk` dep)
- `src/deploy/`: `preflight.py` (SDK auth), `parse_tickets.py`, `load_tables.py`,
  `build_glossary.py` (seed + function), `build_serving_table.py` (soft rows), `build_ka.py`,
  `build_serving_agents.py`, `build_supervisor.py` (KA discovery + suffix), `frontdoor_deploy.py`
  (SDK apps), `render_genie.py` (NEW)
- `data_generation/build_silver.py`, `data/servicenow/*.md` (NEW corpus), `.gitignore`
- `genie/genie_space.template.json` (renamed from genie_space.json, placeholders)
- Docs: `DEPLOYMENT.md`, `dab_instructions.md`, `architecture.md`, `specifications/01-ingest-and-enrich.md`

## Gotchas for the resuming agent
- **CLI `bundle run` stream can time out** on long jobs (KA index ~24 min) — the JOB keeps
  running. Poll `databricks jobs get-run <id>` instead of trusting the stream.
- **Task errors are hidden** in the CLI stream (`SystemExit: N`). Use
  `databricks jobs get-run-output <task_run_id>` for the real error + logs.
- Run long jobs in the background and poll; don't chain `sleep`.
- zsh: don't word-split scalars (pass `--var` flags literally); `UID` is readonly.
- Never auto-select a profile; always `--profile serverless-stable`.
- The whole refactor is **uncommitted** — commit only when the user asks.
