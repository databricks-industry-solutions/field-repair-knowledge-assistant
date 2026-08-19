# Pipeline rebuild — progress (SDP → notebook job)

> Live progress notes for the work that replaced the Lakeflow SDP with a notebook-based
> job so the Knowledge Assistant can stream from `rd_tasks_serving`. Companion to
> `VALIDATION-HANDOFF.md` (the broader end-to-end deployment validation). Delete both once
> validation is complete — they are NOT part of the template.
>
> Last updated: 2026-08-18 ~16:46 local.

## The problem (blocker #15)

The KA attaches to `rd_tasks_serving` as a `file_table` source and its `:sync` does a
**streaming read**. The (uncommitted) SDP refactor built `rd_tasks_serving` as a
**materialized view**, so the agents job failed with:

```
[STREAMING_FROM_MATERIALIZED_VIEW] Cannot stream from Materialized View
`serverless_stable_l26d62_catalog`.`fis_knowledge_agent_dev`.`rd_tasks_serving`.
```

CDF-on does **not** make an MV streamable. GATE 2 only checked "CDF enabled", so it passed
and masked this.

## Decision (user-approved)

Drop SDP entirely. Rebuild the enrichment + serving stages as **serverless
`notebook_task` notebooks** in the existing `fis_data_pipeline` job. `rd_tasks_serving`
becomes a **plain Delta table** (CDF on) — streamable. Incremental enrichment preserved via
the `content_hash` LEFT ANTI JOIN + `MERGE` (not SDP streaming). Approved plan:
`~/.claude/plans/you-are-right-that-radiant-penguin.md`.

## Status

| # | Step | Status |
|---|------|--------|
| 1 | Author notebooks + rewrite DAG + delete SDP + update docs | ✅ done |
| 2 | `bundle validate` | ✅ pass |
| 3 | Notebooks compile + `enrich_recipe` smoke-test | ✅ pass |
| 4 | `bundle deploy` (data infra) | ✅ done (after clearing orphaned-pipeline state) |
| 5 | Run `fis_data_pipeline` (run `78443158871374`) | ✅ **all 6 tasks SUCCESS** |
| 6 | `rd_tasks_serving` is a plain Delta table | ✅ `table_type = MANAGED`; `serving` verify() PASSED (incl. new table-type check) |
| 7 | Delete FAILED dev KA `ebc4e83f` (rebuild fresh) | ✅ done |
| 8 | Run `fis_agents` (run `906615799025620`) | ✅ **KA blocker RESOLVED** — KA recreated (`343d14e9`) and synced **ACTIVE against the plain table in 172s** (no `STREAMING_FROM_MATERIALIZED_VIEW`). Task exited non-zero only on 2 verify assertions unrelated to the fix (see below). |
| 8b | Fix the 2 verify assertions + re-run (run `97875158199499`) | ✅ `serving_agents` SUCCESS; `supervisor` FAILED → root-caused 2 more blockers (#16, #17) |
| 8c | Fix supervisor blockers #16 (FQN retarget) + #17 (tools sub-resource API) | ✅ **DONE** — full local build via the script passed end-to-end (3 tools bound, ONLINE, grants asserted, **smoke PASS**) |
| 9 | GATE 3: re-deploy + re-run `fis_agents` in-job | ✅ **PASSED** (run `677357740766336`, both tasks SUCCESS, supervisor smoke PASS on serverless). MAS = `fis-rnd-supervisor-dev` id `b5da5fdd-cdd5-4de7-85e8-d9c252b75fe8`, endpoint **`mas-b5da5fdd-endpoint`**. |
| 10 | Stage 4 — front-door app (GATE 4) | ✅ **PASSED** — app `fis-rnd-frontdoor-dev` RUNNING, serving-endpoint → `mas-b5da5fdd-endpoint` (CAN_QUERY), `user_api_scopes=[serving.serving-endpoints]`. Fixed blocker #18 (SDK `apps.update` keyword-only `app` arg). Order: `bundle run frontdoor` (start) THEN `fis_frontdoor_authz` (bind+verify) — the handoff's order was reversed. |
| 11 | Stage 5 — functional tests | ✅ **PASSED** — all 3 harnesses retargeted to `-dev` + green: `test_ka` 3/3, `test_genie` 10/10, `test_supervisor` 17/17 (routing + fan-out + A4/A5 structure). See blocker #19. |
| 12 | Cleanup (bundle destroy + Agent-Bricks assets) | ⛔ pending user go-ahead |

## ✅ Blockers #16 + #17 fixed — the supervisor binds its tools and answers live

Run 8b's `supervisor` task exited 7 (grant assertion), and once that was fixed,
exit 8 (smoke test). Two independent root causes, both in `build_supervisor.py`:

**#16 — grant/probe hit the WRONG schema on a `--catalog/--schema` retarget.**
`_apply_target` rewrites fully-qualified names by diffing the module's `FQ` global,
but `build_supervisor.py` never defined a module-level `FQ` (unlike
`build_serving_agents.py` / `build_glossary.py`). So `old_fq` was `None`, the rewrite
loop no-op'd, and `GLOSSARY_FN` / `ANALYTICS_VIEW` stayed pointed at the default demo
schema (`main.troubleshooting_knowledge_agent.*`) — the grants/probe false-failed.
**Fix:** added `FQ = f"{CATALOG}.{SCHEMA}"` at module scope (mirrors the sibling
modules), so the existing rewrite retargets both FQNs.

**#17 — the MAS came up with ZERO callable tools ("Error: Tool 'X' not found").**
The script sent an inline `agents[]` list on the create body. The **current**
Supervisor Agent API ignores that — tools are separate sub-resources:
`POST /supervisor-agents/{id}/tools?tool_id=<name>` with `{tool_type, description,
<type-block>}` (`knowledge_assistant.knowledge_assistant_id` / `genie_space.id` /
`uc_function.name`), and examples are `POST …/examples` with `{question,
guidelines[]}`. The LLM saw the tool *names* but every call errored "not found"
because nothing was bound. **Fix:** rewrote `create_or_update_mas` to create the MAS
bare, then `reconcile_tools` (attach-if-missing, all 3) + `reconcile_examples`
(wraps the legacy single `guideline` into `guidelines[]`) before polling. KA now
binds by its discovered **id** (`resolve_ka` returns id + endpoint). Wire shapes
confirmed live via the `databricks supervisor-agents` CLI + `--debug` (tool_id is a
query param). `--recreate` deletes + re-attaches to change a binding.

## ✅ Blocker #15 is fixed — the KA streams from the plain Delta table

Run 8 log (`serving_agents`):
```
[pre]   metadata ✓  ka_content ✓  table-type ✓  CDF ✓  analytics view ✓ (23 rows)
[KA] Created: knowledge-assistants/343d14e9-... state=CREATING
[KA] t+103s KA=ACTIVE sources={'rd_tasks_serving_corpus': 'UPDATED', ...}
[KA] ACTIVE, all sources UPDATED in 172s.
  [PASS] KA state == ACTIVE
  [PASS] KA corpus points at rd_tasks_serving
  [PASS] all KA sources UPDATED
```
The exact failure mode (`STREAMING_FROM_MATERIALIZED_VIEW`) is gone. Everything below is
verify-harness cleanup, not the fix.

### Two verify assertions the run tripped on (now fixed in `build_serving_agents.py`)
1. `examples attached == 8 (got 3)` — the LLM-backed example endpoint timed out on 5/8
   attaches (`Timed out after 0:05:00`). Fix: `attach_examples` now **retries** each attach
   3× with backoff; verify treats a PARTIAL attach as a **WARN** (KA is ACTIVE and answers),
   hard-fails only on ZERO examples.
2. `EXISTING KA still on rnd_tickets (untouched)` — the check found the **shared-demo** KA
   (`fis-rnd-knowledge-assistant` on schema `fis_knowledge_agent`) and wrongly asserted it
   points at the **dev** schema's `rnd_tickets`. Fix: assert the legacy KA is still on *its
   own* `.rnd_tickets` (name ends in `.rnd_tickets`), not this deploy's schema — so an
   isolated/suffixed deploy no longer false-fails.

## What changed (files)

**Created** — `src/notebooks/`:
- `enrich.py` — notebook: builds `rd_tasks_gold_enrichment` via `ai_query`, incremental
  (`content_hash` anti-join + `MERGE`; 0 new tickets ⇒ 0 model calls).
- `serving.py` — notebook: builds `rd_tasks_serving` as `CREATE OR REPLACE TABLE …
  TBLPROPERTIES(delta.enableChangeDataFeed=true)` (plain, streamable), then the Genie
  analytics views, then `verify()` (with the new table-type check).
- `enrich_recipe.py` — moved here from `src/pipeline/transformations/` (single-source
  prompt/schema/acronym logic; both notebooks import it).

**Deleted** — `resources/enrich_pipeline.pipeline.yml`; `src/pipeline/` (whole SDP).

**Edited**:
- `resources/jobs_pipeline.yml` — DAG now `parse → load → {build_silver, glossary} →
  enrich (notebook) → serving (notebook)`; no `pipeline_task`.
- `src/deploy/build_serving_agents.py` — added the table-type assertion to `check_prereqs`;
  fixed the "built by the SDP pipeline" docstring.
- `src/deploy/build_serving_table.py` — demoted to a LOCAL `--verify`/`--analytics-only`
  CLI (no longer a job task); added the table-type check to `verify()`.
- `databricks.yml`, `DEPLOYMENT.md`, `dab_instructions.md`, `architecture.md`,
  `specifications/01-ingest-and-enrich.md`, `specifications/02-agents.md`,
  `data_generation/build_silver.py` — removed SDP/MV language; documented notebook job +
  the "KA can't stream from an MV" constraint.

Notebooks are Databricks **source-format `.py`** (`# Databricks notebook source` +
`# COMMAND ----------`) — same `notebook_task`/serverless execution, diffable in git. DAB
imported them as notebooks (verified: deployed job references `.../src/notebooks/enrich`,
`.../serving` with the extension dropped).

## Deployment coordinates (this validation)

- Profile `serverless-stable` · target `dev` · catalog `serverless_stable_l26d62_catalog`
  · schema `fis_knowledge_agent_dev` · warehouse `04a4dee7888b9e64` · agent_suffix `-dev`.
- Data job `fis_data_pipeline` = `860640035735815`. Agents job `fis_agents` =
  `35352398233080`.

## Gotcha logged

Removing the SDP left an orphaned `fis_enrich_pipeline` in the bundle's **direct-backend**
state (CLI v1.3.0: `resources.json`, not `.tfstate`), which broke `bundle deploy` with
`failed to compute relative path for pipeline`. Fixed by deleting the workspace pipeline
and removing the `resources.pipelines.fis_enrich_pipeline` key from
`.databricks/bundle/dev/resources.json`, then re-deploying (backup at
`/tmp/dab_resources_local.bak`).

## Next actions (when run 8 finishes)

1. If `serving_agents` SUCCESS → the KA is ACTIVE against the plain table ⇒ **blocker #15
   resolved.** Note the MAS endpoint the `supervisor` task printed.
2. **GATE 3:** `python3 src/deploy/build_serving_agents.py --profile serverless-stable
   --verify --catalog serverless_stable_l26d62_catalog --schema fis_knowledge_agent_dev
   --warehouse-id 04a4dee7888b9e64 --agent-suffix=-dev`.
3. Resume `VALIDATION-HANDOFF.md`: Stage 4 (front-door app) → Stage 5 (functional tests;
   these still need retargeting to the `-dev` endpoints — see that file) → cleanup.
4. If `serving_agents` FAILS again → pull `jobs get-run-output <task_run_id>` for the real
   error (the CLI stream hides it behind `SystemExit`).
