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

> **Auth expiry is a real failure mode.** A previous unattended run had its OAuth
> refresh token expire ~137s into a 25-minute KA poll. The poller reported
> `KA=UNKNOWN sources={}` for 8 minutes, which looked like a broken KA but was
> blind auth errors. If you ever see `UNKNOWN` or an empty source map, check
> `databricks auth token --profile <PROFILE>` **before** concluding anything about
> the KA.

---

## Stage 1 — Deploy the bundle (creates nothing expensive)

```bash
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
create genie_spaces.fis_rnd_serving
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
databricks bundle deploy -t <TARGET> \
  --var catalog=<CATALOG> --var schema=<SCHEMA> --var warehouse_id=<WAREHOUSE_ID>
```

**GATE 1**

```bash
databricks bundle summary -t <TARGET>
```

Expect 7 resources: `schemas.fis`, `volumes.glossary`, `genie_spaces.fis_rnd_serving`,
`apps.frontdoor`, and jobs `fis_data_pipeline`, `fis_agents`, `fis_frontdoor_authz`.
Anything missing → stop.

---

## Stage 2 — Data pipeline

```bash
databricks bundle run fis_data_pipeline -t <TARGET>
```

7 tasks in order: `parse_tickets` → `load_tables` → `silver` → `glossary` →
`enrich` → `ka_content` → `serving_table`. The `enrich` task calls an LLM per
ticket; on a first run budget ~10 min, on a re-run it is near-instant because the
`content_hash` anti-join finds nothing to do.

**GATE 2 — the serving table must satisfy BOTH engines.**

```bash
python3 src/deploy/build_serving_table.py --profile <PROFILE> --verify \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>
```

Requires **11/11 PASS** and the line `VERIFY PASSED`. The three that block Stage 3:

| Check | Why it blocks |
|---|---|
| `metadata struct present` | KA attach fails with `missing required column '_metadata'` |
| `CDF enabled` | KA attach fails: *"must either be a streaming table or have Change Data Feed enabled"* |
| `ka_content present` | `file_col` needs exactly one pre-composed column |

Also confirm the glossary actually has approved terms — enrichment enums are built
from them at run time, and an empty glossary means an uncontrolled vocabulary:

```bash
python3 src/deploy/build_glossary.py --profile <PROFILE> --verify-only \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>

# GLO-02 coupling: enrichment's system values == the approved glossary set, both ways.
python3 src/deploy/enrich.py --profile <PROFILE> --drift-guard \
  --catalog <CATALOG> --schema <SCHEMA> --warehouse-id <WAREHOUSE_ID>
```

**GATE 2b** — `--drift-guard` reports no drift in either direction.

---

## Stage 3 — Agents (the long one)

```bash
databricks bundle run fis_agents -t <TARGET>
```

Two tasks: `serving_agents` (KA + Genie) then `supervisor`.

**Expect ~25 minutes.** A measured run reached `ACTIVE` with both sources
`UPDATED` at **1434s**. Poll; do not assume. Healthy intermediate output looks like:

```
[KA] t+400s KA=CREATING sources={'rd_tasks_serving_corpus': 'UPDATING', 'fis_glossary': 'UPDATING'}
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
# OBO scopes first — the DAB App resource cannot express user_api_scopes.
databricks bundle run fis_frontdoor_authz -t <TARGET>

# The app needs an explicit run to start.
databricks bundle run frontdoor -t <TARGET>
```

**GATE 4**

```bash
databricks apps get fis-rnd-frontdoor --profile <PROFILE> | grep -E '"state"|"url"'
```

Must be `RUNNING`. Then confirm the OBO wiring actually landed — this is the
step DAB cannot do, so it is the step most likely to be silently missing:

```bash
databricks apps get fis-rnd-frontdoor --profile <PROFILE> \
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

## Stage 6 — Report

Produce a table of every gate with its **observed** result, then state plainly:

**What you could NOT verify.** At minimum this includes:

- **The app UI.** `curl` gets a 302 SSO redirect, so a human with a browser session
  must confirm the chat renders, the progress line ticks through its stages, and
  citation chips are clickable.
- Anything you skipped, with the reason.

Then ask the human for the two things only they can do:

1. Click through the front door and confirm the four demo acts in `README.md`.
2. Capture `template_screenshot.png` (still missing — every reference template
   ships one, ~180KB). Front-door chat mid-answer with citation chips visible; that
   image is what sells the demo in the catalog list. Do not fabricate it.

### Placeholders to confirm before submitting

- `manifest.json` leaves `customer` empty — correct for a generic template. Set it
  only for an account-specific variant.
- The `specifications/` files use `{{CATALOG}}` / `{{SCHEMA}}`, matching the
  reference-template convention. Leave them as placeholders.

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
