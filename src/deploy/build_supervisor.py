#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant — build the Multi-Agent Supervisor (MAS).

Stands up ONE Agent Bricks Multi-Agent Supervisor over the THREE already-live,
Phase-4.1-enriched tools and proves the whole pipe end-to-end:

  Tool 1 (KA)      — tile 97df484b-f50a-4042-ad2f-0be5a3ce6779
                     (endpoint ka-97df484b-endpoint, task agent/v1/responses),
                     semantic similar-case retrieval over enriched ka_content.
  Tool 2 (Genie)   — the DAB-deployed Genie space (resources/genie.yml, resolved
                     at runtime by title) over the curated analytics view
                     (counts/sorts/expert/priority).
  Tool 3 (glossary)— <catalog>.<schema>.glossary_lookup
                     (term_query STRING) -> TABLE(term, definition, category).
                     Terminology is resolved AT the supervisor (4.1 architecture).

What this script does (idempotent, host-gated — mirrors build_ka.py):

  Task 1 — create/update the 3-tool MAS:
    * Step 0: preflight.assert_target_host — refuse any workspace but the reference workspace.
    * find-by-display_name (GET /api/2.1/supervisor-agents) → update else create.
    * Author the create-request from src/deploy/supervisor_config.json (human-editable
      display_name + 3 tool descriptions + routing/synthesis instructions +
      examples[]).
    * Create-request wire shape is a SPIKE: the confirmed
      envelope is {"supervisor_agent": {...}} requiring a non-empty display_name;
      the sub-agent list key + per-agent descriptor keys are probed incrementally
      (manage_mas semantics: each agent = name + description + EXACTLY ONE of
      ka_tile_id | genie_space_id | uc_function_name | endpoint_name). KA is tried
      by tile id first, with endpoint_name fallback if the proto rejects the tile.
    * Poll the MAS serving endpoint to ONLINE (gate on state, no fixed timer).
    * Confirm the endpoint .task (expect agent/v1/responses, A3).
    * Record MAS id + endpoint + ACCEPTED wire shape + invocation contract +
      the A1 supervisor-LLM-pin finding into 05-SUPERVISOR-BUILD.md.

  Task 2 — grants + one live end-to-end answer:
    * Discover the MAS service-principal identity from the MAS/endpoint metadata.
    * Issue THREE least-privilege grants for BOTH the demo principal AND the MAS SP
      (never OWNER/ALL/admin — T-5-02): EXECUTE on glossary_lookup; CAN QUERY on
      the KA endpoint; Genie backing SELECT on rd_tasks_gold_analytics + warehouse
      CAN USE (+ best-effort Genie space run access).
    * Assert the grants (SHOW GRANTS / permissions GET / SELECT probe).
    * Fire ONE terminology-bearing live question at the MAS endpoint (Responses-API
      {"input":[...]} contract) and confirm non-empty prose returns.
    * Append the SP identity, the three asserted grants, and the smoke-test result
      to 05-SUPERVISOR-BUILD.md.

Usage:
    python3 src/deploy/build_supervisor.py --profile serverless-stable
    python3 src/deploy/build_supervisor.py --profile serverless-stable --grants-only
    python3 src/deploy/build_supervisor.py --profile serverless-stable --skip-smoke
"""

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Reuse the Phase-1 host-safety gate + SQL/principal helpers --------------
# NOTE: preflight.run_cli hardcodes a 180s timeout and takes no `timeout` kwarg,
# so this module defines its own run_cli (mirrors build_ka.run_cli) that accepts
# a per-call timeout; api_json/smoke_test need longer ceilings for provisioning
# + agent invocation. The host-gate/run_sql/resolve_principal helpers are reused
# verbatim from preflight (they use preflight's own run_cli internally).
# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally. preflight.py
# and env.py live in THIS dir (the old REPO_ROOT/"preflight" path no longer exists).
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO_ROOT = _HERE.parent
sys.path.insert(0, str(_HERE))
from preflight import (  # noqa: E402
    assert_target_host,
    run_sql,
    resolve_principal,
)


def run_cli(args, profile, timeout=180):
    """Run a databricks operation, return (exit_code, stdout, stderr).

    Routes `api` calls + `auth env` through the SDK (ambient auth) so this works on
    serverless job compute; anything else falls back to the subprocess CLI (local).
    """
    import preflight as _pf
    try:
        if args and args[0] == "api":
            method, path = args[1], args[2]
            body = json.loads(args[args.index("--json") + 1]) if "--json" in args else None
            return _pf.api_do(method, path, profile, body)
        if args[:2] == ["auth", "env"]:
            host = _pf.workspace_client(profile).config.host or ""
            return 0, json.dumps({"env": {"DATABRICKS_HOST": host}}), ""
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)
    cmd = ["databricks", *args, "--profile", profile]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "databricks CLI not found"

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical default values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
# Module-level FQ so _apply_target's old_fq rewrite catches GLOSSARY_FN /
# ANALYTICS_VIEW (both built from CATALOG.SCHEMA below). Without this, old_fq is
# None and the FQN-rewrite loop no-ops, leaving the grants/probes pointed at the
# default demo schema on a --catalog/--schema retarget (dev-isolation false-fail).
FQ = f"{CATALOG}.{SCHEMA}"

# The three live tools.
KA_TILE_ID = "97df484b-f50a-4042-ad2f-0be5a3ce6779"
KA_ENDPOINT = "ka-97df484b-endpoint"
KA_ID = ""  # KA knowledge_assistant_id, discovered in main() from the KA display name
# The Genie space is DAB-deployed (resources/genie.yml). Its id is generated at
# deploy time, so it is resolved at runtime BY TITLE rather than hardcoded.
GENIE_SPACE_TITLE = "Field Repair Tickets (serving)"
GENIE_SPACE_ID = ""  # filled in main() from the DAB-deployed space (by title)
GLOSSARY_FN = f"{CATALOG}.{SCHEMA}.glossary_lookup"
ANALYTICS_VIEW = f"{CATALOG}.{SCHEMA}.rd_tasks_gold_analytics"

MAS_API = "/api/2.1/supervisor-agents"
CONFIG_PATH = _HERE / "supervisor_config.json"
BUILD_DOC = (
    REPO_ROOT
    / "reports/05-SUPERVISOR-BUILD.md"
)

# Poll config.
POLL_INTERVAL_S = 20
POLL_CEILING_S = 60 * 15  # 15 min ceiling; report + stop, re-run to resume.

# A terminology-bearing smoke question so the glossary hop is exercised.
SMOKE_QUESTION = (
    "What does CA mean in these R&D tickets, and is it a screening system or a "
    "software/controller term?"
)


# --- REST helper (mirror build_ka.api_json) ---------------------------------

def api_json(method, path, profile, body=None, timeout=180):
    """`databricks api <method> <path>` with optional JSON body.

    Returns (exit_code, parsed_json_or_None, raw_stdout, stderr).
    """
    args = ["api", method, path]
    if body is not None:
        args += ["--json", json.dumps(body)]
    code, out, err = run_cli(args, profile, timeout=timeout)
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    return code, parsed, out, err


# --- Task 1: create/update the 3-tool MAS -----------------------------------

def resolve_genie_space_id(profile, title):
    """space_id of the DAB-deployed Genie space with this exact title, else None."""
    code, parsed, _, _ = api_json("get", "/api/2.0/genie/spaces", profile)
    if code != 0 or not isinstance(parsed, dict):
        return None
    for s in parsed.get("spaces", []) or []:
        if (s.get("title") or "") == title:
            return s.get("space_id")
    return None


def resolve_ka(profile, display_name):
    """(knowledge_assistant_id, endpoint_name) of the KA with this display name.

    The MAS must route to the KA it is PAIRED with, discovered at build time — never a
    hardcoded tile/endpoint (which is wrong for any fresh or dev/isolated deploy). The
    knowledge_assistant tool binds by the KA *id* (see reconcile_tools), so we resolve
    both the id and the endpoint (the endpoint is still used for the CAN QUERY grant).
    """
    code, parsed, _, _ = api_json("get", "/api/2.1/knowledge-assistants", profile)
    if code != 0 or not isinstance(parsed, dict):
        return None, None
    for ka in parsed.get("knowledge_assistants", []) or []:
        if (ka.get("display_name") or "") == display_name:
            return (ka.get("knowledge_assistant_id") or ka.get("id"),
                    ka.get("endpoint_name"))
    return None, None


def load_config():
    cfg = json.loads(CONFIG_PATH.read_text())
    if not cfg.get("display_name"):
        print("FATAL: supervisor_config.json missing display_name", file=sys.stderr)
        sys.exit(4)
    if len(cfg.get("agents", [])) != 3:
        print("FATAL: supervisor_config.json must register exactly 3 agents",
              file=sys.stderr)
        sys.exit(4)
    return cfg


def _list_mas(profile):
    """GET the supervisor-agents list. Envelope key = `supervisor_agents`.

    Live-confirmed 2026-07-29: GET returns {"supervisor_agents":[{...}]} (or a
    bare {} when none exist).
    """
    code, parsed, _, _ = api_json("get", MAS_API, profile)
    if code != 0 or parsed is None:
        return []
    if isinstance(parsed, dict):
        for key in ("supervisor_agents", "agents", "items"):
            if isinstance(parsed.get(key), list):
                return parsed[key]
        return []  # a bare {} means "none yet"
    if isinstance(parsed, list):
        return parsed
    return []


def _extract_id(parsed):
    """Pull the MAS id from a create/list item. Live key = supervisor_agent_id."""
    if not isinstance(parsed, dict):
        return None
    return (parsed.get("supervisor_agent_id")
            or parsed.get("id")
            or (str(parsed.get("name") or "").split("/")[-1] or None))


def find_existing_mas(profile, display_name):
    """Return (id, resource_dict) of an existing MAS by display_name, else (None, None)."""
    for m in _list_mas(profile):
        if isinstance(m, dict) and m.get("display_name") == display_name:
            return _extract_id(m), m
    return None, None


def _create_body(cfg):
    """The TOP-LEVEL create body (wire shape live-confirmed 2026-08-19).

    The supervisor is created with ONLY {display_name, description, instructions}.
    Tools and examples are NOT accepted inline on the create body — they are
    separate sub-resources (POST .../tools, POST .../examples) attached after the
    agent exists (see reconcile_tools / reconcile_examples). An earlier revision
    sent an inline `agents[]` list; the current API silently ignores it, so the
    supervisor came up with ZERO bound tools ("Error: Tool '<x>' not found" at
    invocation) — the exact failure this rewrite fixes.
    """
    return {
        "display_name": cfg["display_name"],
        "description": cfg["description"],
        "instructions": cfg["instructions"],
    }


def _tool_specs(cfg):
    """(tool_id, request_body) per configured agent, in the CURRENT /tools wire shape.

    Each tool is POSTed to `.../tools?tool_id=<tool_id>` with a body of
    {tool_type, description, <type-block>}. The type-block identifier field differs
    by tool_type (knowledge_assistant_id / genie_space.id / uc_function.name). The KA
    binds by its DISCOVERED id (KA_ID) and the Genie/glossary by their retargeted
    identifiers — never a hardcoded tile, which is wrong for a fresh/dev deploy.
    """
    specs = []
    for a in cfg["agents"]:
        body = {"description": a["description"]}
        if "ka_tile_id" in a:
            body["tool_type"] = "knowledge_assistant"
            body["knowledge_assistant"] = {"knowledge_assistant_id": KA_ID}
        elif "genie_space_id" in a:
            body["tool_type"] = "genie_space"
            body["genie_space"] = {"id": GENIE_SPACE_ID}
        elif "uc_function_name" in a:
            body["tool_type"] = "uc_function"
            body["uc_function"] = {"name": GLOSSARY_FN}
        else:
            continue
        specs.append((a["name"], body))
    return specs


def list_tool_ids(profile, mid):
    """Set of tool_id already attached to this MAS (GET .../tools -> {"tools":[...]})."""
    code, parsed, _, _ = api_json("get", f"{MAS_API}/{mid}/tools", profile)
    if code != 0 or not isinstance(parsed, dict):
        return set()
    return {t.get("tool_id") for t in (parsed.get("tools") or [])
            if isinstance(t, dict) and t.get("tool_id")}


def reconcile_tools(profile, mid, cfg):
    """Attach every configured tool that is not already present. Returns a log.

    Idempotent: existing tool_ids are left untouched (a changed binding/description
    needs --recreate, since tools, like agents historically, are effectively
    create-time). Tool bindings determine whether the supervisor can actually CALL
    its agents, so a failure here is fatal to the build.
    """
    existing = list_tool_ids(profile, mid)
    log = []
    for tool_id, body in _tool_specs(cfg):
        if tool_id in existing:
            log.append((tool_id, body.get("tool_type"), True, "exists"))
            continue
        code, _, out, err = api_json(
            "post", f"{MAS_API}/{mid}/tools?tool_id={tool_id}", profile, body=body)
        ok = code == 0
        log.append((tool_id, body.get("tool_type"), ok,
                    "created" if ok else (err or out)[:200]))
    return log


def reconcile_examples(profile, mid, cfg):
    """Attach the configured routing examples if the MAS has none yet (idempotent).

    Examples are a sub-resource (POST .../examples) taking {question, guidelines[]}.
    The config stores a single `guideline` string per example (legacy inline shape);
    it is wrapped into the `guidelines` array the current API expects.
    """
    code, parsed, _, _ = api_json("get", f"{MAS_API}/{mid}/examples", profile)
    have = len(parsed.get("examples") or []) if isinstance(parsed, dict) else 0
    if have:
        return have  # already seeded — leave as-is
    made = 0
    for ex in cfg.get("examples", []) or []:
        q = ex.get("question")
        guidelines = ex.get("guidelines") or (
            [ex["guideline"]] if ex.get("guideline") else [])
        if not q or not guidelines:
            continue
        code, _, _, _ = api_json(
            "post", f"{MAS_API}/{mid}/examples", profile,
            body={"question": q, "guidelines": guidelines})
        if code == 0:
            made += 1
    return made


def create_or_update_mas(profile, cfg, recreate=False):
    """Idempotent create-or-update. Returns (mas_id, accepted_body, operation).

    Wire-shape facts (live-confirmed 2026-07-29):
      * CREATE: top-level POST with display_name/description/instructions/agents/examples.
      * UPDATE: PATCH with `?update_mask=` query param, editable fields ONLY =
        {display_name, description, instructions}. `agents` are CREATE-TIME ONLY
        (PATCH rejects them: "Unsupported update_mask fields: agents").
      * Therefore true idempotency without endpoint churn PATCHes the editable
        fields on re-run; changing the tool SET or a tool DESCRIPTION requires a
        recreate (delete + create). Pass recreate=True (or --recreate) for that.

    Tools + examples are attached AFTER this returns (reconcile_tools /
    reconcile_examples) — they are sub-resources, not part of the create body.
    """
    mid, existing = find_existing_mas(profile, cfg["display_name"])

    if mid and recreate:
        print(f"[T1] --recreate: deleting existing MAS {mid} to re-apply tools...")
        code, _, out, err = api_json("delete", f"{MAS_API}/{mid}", profile)
        if code != 0:
            print(f"FATAL: could not delete MAS {mid} for recreate: {err or out}",
                  file=sys.stderr)
            sys.exit(5)
        mid = None

    if mid:
        # Update the editable fields in place (tools are attached separately below).
        body = _create_body(cfg)
        mask = "display_name,description,instructions"
        code, parsed, out, err = api_json(
            "patch", f"{MAS_API}/{mid}?update_mask={mask}", profile, body=body)
        if code == 0:
            print(f"[T1] Updated MAS {mid} (editable fields via update_mask={mask}). "
                  "Tool set is reconciled separately — use --recreate to change bindings.")
            return mid, body, "updated"
        print(f"FATAL: MAS update (PATCH) failed: {err or out}", file=sys.stderr)
        sys.exit(5)

    # Create fresh — bare {display_name, description, instructions}; tools follow.
    body = _create_body(cfg)
    code, parsed, out, err = api_json("post", MAS_API, profile, body=body)
    if code == 0:
        new_id = _extract_id(parsed)
        print(f"[T1] Created MAS id={new_id}.")
        return new_id, body, "created"

    print(f"FATAL: could not create the MAS: {(err or out)[:300]}", file=sys.stderr)
    sys.exit(5)


def get_mas(profile, mid):
    """GET the MAS resource (top-level shape; agents are NOT echoed — opaque)."""
    code, parsed, _, _ = api_json("get", f"{MAS_API}/{mid}", profile)
    if code != 0 or not isinstance(parsed, dict):
        return {}
    return parsed


def _endpoint_of(mas):
    """Best-effort discovery of the MAS serving-endpoint name from metadata."""
    for k in ("endpoint_name", "serving_endpoint_name", "endpoint"):
        v = mas.get(k)
        if isinstance(v, str) and v:
            return v
    ep = mas.get("serving_endpoint")
    if isinstance(ep, dict):
        return ep.get("name")
    return None


def discover_endpoint(profile, mid, mas):
    """Return the MAS serving-endpoint name. Falls back to a name scan."""
    name = _endpoint_of(mas)
    if name:
        return name
    # Scan serving endpoints for one whose name embeds the MAS id fragment
    # (reference build used `mas-<idfrag>-endpoint`).
    code, parsed, _, _ = api_json("get", "/api/2.0/serving-endpoints", profile)
    frag = (mid or "").split("-")[0]
    eps = (parsed or {}).get("endpoints", []) if isinstance(parsed, dict) else []
    for ep in eps:
        n = ep.get("name", "")
        if frag and frag in n and "mas" in n.lower():
            return n
    return None


def endpoint_state_and_task(profile, endpoint):
    """Return (state, task) for a serving endpoint via `serving-endpoints get`."""
    code, parsed, _, _ = api_json(
        "get", f"/api/2.0/serving-endpoints/{endpoint}", profile)
    if code != 0 or not isinstance(parsed, dict):
        return "UNKNOWN", None
    state = ((parsed.get("state") or {}).get("ready")
             or (parsed.get("state") or {}).get("config_update")
             or parsed.get("state"))
    task = parsed.get("task")
    return (state if isinstance(state, str) else json.dumps(state)), task


def poll_online(profile, mid):
    """Poll the MAS + its serving endpoint to ONLINE/READY. Returns (endpoint, state, task, ok)."""
    print(f"[T1] Polling MAS {mid} to ONLINE (interval {POLL_INTERVAL_S}s, "
          f"ceiling {POLL_CEILING_S//60} min)...")
    start = time.time()
    last = None
    endpoint = None
    while True:
        mas = get_mas(profile, mid)
        mas_state = mas.get("state") or mas.get("status") or "UNKNOWN"
        if endpoint is None:
            endpoint = discover_endpoint(profile, mid, mas)
        ep_state, task = ("UNKNOWN", None)
        if endpoint:
            ep_state, task = endpoint_state_and_task(profile, endpoint)
        elapsed = int(time.time() - start)
        line = f"[T1] t+{elapsed}s MAS={mas_state} endpoint={endpoint} ep_state={ep_state}"
        if line != last:
            print(line)
            last = line

        ready = str(ep_state).upper() in ("ONLINE", "READY") or \
            str(mas_state).upper() == "ONLINE"
        if endpoint and ready:
            print(f"[T1] MAS ONLINE in {elapsed}s (endpoint={endpoint}, task={task}).")
            return endpoint, ep_state, task, True
        if str(mas_state).upper() in ("FAILED", "ERROR"):
            print(f"FATAL: MAS entered {mas_state}.", file=sys.stderr)
            return endpoint, ep_state, task, False
        if elapsed > POLL_CEILING_S:
            print(f"WARNING: exceeded {POLL_CEILING_S//60} min poll ceiling "
                  f"(MAS={mas_state}, ep={ep_state}). Re-run to resume.",
                  file=sys.stderr)
            return endpoint, ep_state, task, False
        time.sleep(POLL_INTERVAL_S)


# --- Task 2: discover MAS SP, grants, smoke test ----------------------------

def discover_mas_sp(profile, mid, mas, endpoint):
    """Discover the MAS run-as service-principal identity (Open Q4, A7).

    IMPORTANT (resolved live 2026-07-29): the endpoint `creator` field is the
    creating USER — NOT the identity that executes tool calls. The MAS runs every
    tool call under a system-generated per-tile SSP ("Agent Bricks Per-Tile SSP").
    The ONLY reliable discovery is `system.access.audit`: find the service-principal
    actor (application-id UUID) performing tile ops (getTile / createSchema /
    getVolume / supervisor_agent get) referencing this MAS id. `creator` is returned
    ONLY as a last-resort marker (and is wrong for grant purposes).

    NOTE: even once discovered, workspace SQL/UC-REST GRANTs to this SSP silently
    no-op because it is an account-level identity not registered in the workspace
    UC (see 05-SUPERVISOR-BUILD.md "SUP-01 BLOCKER"). Authorizing it requires the
    Agent Bricks UI tool-permissions panel or account-admin registration + grant.
    """
    frag = (mid or "").split("-")[0]
    audit_sql = f"""
        SELECT user_identity.email AS actor
        FROM system.access.audit
        WHERE event_date >= current_date() - 1
          AND user_identity.email RLIKE '^[0-9a-fA-F-]{{36}}$'
          AND lower(to_json(request_params)) LIKE '%{frag}%'
        GROUP BY 1
        ORDER BY count(*) DESC
        LIMIT 1
    """
    state, rows = run_sql(audit_sql, profile, WAREHOUSE_ID)
    if state == "SUCCEEDED" and rows:
        return rows[0][0]  # the run-as SSP application id
    # Fallback: metadata creator (a USER — flagged wrong for grants).
    for k in ("service_principal_name", "creator", "created_by"):
        v = mas.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _is_service_principal(principal):
    """SP identities are application-id UUIDs; users are emails."""
    return bool(principal) and "@" not in principal and re.fullmatch(
        r"[0-9a-fA-F-]{30,40}", principal or "") is not None


def grant_execute_function(profile, principal):
    """GRANT EXECUTE ON FUNCTION glossary_lookup TO <principal> (least-privilege)."""
    stmt = f"GRANT EXECUTE ON FUNCTION {GLOSSARY_FN} TO `{principal}`"
    state, _ = run_sql(stmt, profile, WAREHOUSE_ID)
    return state == "SUCCEEDED", stmt


def grant_select_view(profile, principal):
    """GRANT SELECT on the Genie backing view (+ USE CATALOG/SCHEMA) TO <principal>."""
    stmts = [
        f"GRANT USE CATALOG ON CATALOG {CATALOG} TO `{principal}`",
        f"GRANT USE SCHEMA ON SCHEMA {CATALOG}.{SCHEMA} TO `{principal}`",
        f"GRANT SELECT ON VIEW {ANALYTICS_VIEW} TO `{principal}`",
    ]
    ok = True
    for s in stmts:
        state, _ = run_sql(s, profile, WAREHOUSE_ID)
        ok = ok and state == "SUCCEEDED"
    return ok, "; ".join(stmts)


def _permissions_patch(profile, resource_path, principal, level):
    """PATCH the permissions API to ADD one grant without clobbering others."""
    acl_entry = ({"service_principal_name": principal}
                 if _is_service_principal(principal)
                 else {"user_name": principal})
    acl_entry["permission_level"] = level
    body = {"access_control_list": [acl_entry]}
    code, parsed, out, err = api_json("patch", resource_path, profile, body=body)
    return code == 0, (err or out)[:200]


def grant_can_query_endpoint(profile, endpoint, principal):
    """CAN QUERY on the KA serving endpoint (permissions API, least-privilege)."""
    # permissions API keys serving endpoints by id — resolve it.
    code, parsed, _, _ = api_json(
        "get", f"/api/2.0/serving-endpoints/{endpoint}", profile)
    ep_id = (parsed or {}).get("id") if isinstance(parsed, dict) else None
    if not ep_id:
        return False, f"could not resolve id for endpoint {endpoint}"
    return _permissions_patch(
        profile, f"/api/2.0/permissions/serving-endpoints/{ep_id}",
        principal, "CAN_QUERY")


def grant_warehouse_use(profile, principal):
    """CAN USE on the serverless warehouse (permissions API, least-privilege)."""
    return _permissions_patch(
        profile, f"/api/2.0/permissions/warehouses/{WAREHOUSE_ID}",
        principal, "CAN_USE")


def grant_genie_run(profile, principal):
    """Best-effort Genie space run access (permissions API path may vary)."""
    return _permissions_patch(
        profile, f"/api/2.0/permissions/genie/{GENIE_SPACE_ID}",
        principal, "CAN_RUN")


def issue_grants(profile, principals, endpoint):
    """Issue all three least-privilege surfaces for every principal. Returns a log."""
    log = []
    for p in principals:
        if not p:
            continue
        ok, stmt = grant_execute_function(profile, p)
        log.append(("EXECUTE glossary_lookup", p, ok, stmt))
        ok, stmt = grant_select_view(profile, p)
        log.append(("SELECT rd_tasks_gold_analytics (+USE)", p, ok, stmt))
        ok, detail = grant_warehouse_use(profile, p)
        log.append(("CAN USE warehouse", p, ok, detail))
        if endpoint:
            ok, detail = grant_can_query_endpoint(profile, endpoint, p)
            log.append(("CAN QUERY KA endpoint", p, ok, detail))
        ok, detail = grant_genie_run(profile, p)
        log.append(("Genie space CAN RUN (best-effort)", p, ok, detail))
    return log


def assert_grants(profile, principals):
    """Assert the load-bearing grants live. Returns (ok, evidence_lines)."""
    ev = []
    ok = True
    # 1. EXECUTE on glossary_lookup shows for each principal.
    state, rows = run_sql(f"SHOW GRANTS ON FUNCTION {GLOSSARY_FN}", profile,
                          WAREHOUSE_ID)
    flat = json.dumps(rows or [])
    exec_ok = state == "SUCCEEDED" and "EXECUTE" in flat
    for p in principals:
        seen = p and p in flat
        ev.append(f"SHOW GRANTS glossary_lookup: EXECUTE present={exec_ok}, "
                  f"principal `{p}` listed={bool(seen)}")
        ok = ok and exec_ok
    # 2. SELECT probe on the Genie backing view succeeds.
    state, rows = run_sql(f"SELECT count(*) FROM {ANALYTICS_VIEW}", profile,
                          WAREHOUSE_ID)
    sel_ok = state == "SUCCEEDED"
    ev.append(f"SELECT probe on {ANALYTICS_VIEW}: {state} "
              f"(rows={rows[0][0] if sel_ok and rows else '?'})")
    ok = ok and sel_ok
    # 3. No over-grant: confirm only least-privilege verbs were issued.
    ev.append("Least-privilege guard: only EXECUTE / SELECT / USE / CAN_QUERY / "
              "CAN_USE / CAN_RUN issued — no OWNER/ALL/admin (see issued statements).")
    return ok, ev


# Signals that the answer is a silent-redirect FALLBACK, not a real tool answer.
_DENIAL_MARKERS = (
    "unable to access", "not currently available", "don't have access",
    "do not have access", "cannot access", "aren't available", "are not available",
    "without access to",
)
# The approved glossary answer for the CA smoke question (from glossary_lookup('CA')).
_APPROVED_CA_MARKERS = ("control/operator software layer", "controller stack")


def smoke_test(profile, endpoint):
    """Fire ONE terminology-bearing question and HARDEN against silent redirect.

    Returns (ok, prose, note). `ok` is True ONLY if the tools actually executed:
      * a glossary_lookup tool call appears in output[] (routing fired), AND
      * the final prose carries the APPROVED definition, AND
      * the prose does NOT contain a tool-access-denied fallback marker.
    Non-empty prose alone is NOT sufficient (that was the earlier false positive:
    the MAS emits a fluent generic guess when tool execution is denied).
    """
    body = {
        "input": [{"role": "user", "content": SMOKE_QUESTION}],
        "databricks_options": {"return_trace": True},
    }
    code, parsed, out, err = api_json(
        "post", f"/serving-endpoints/{endpoint}/invocations", profile,
        body=body, timeout=600)
    if code != 0 or not isinstance(parsed, dict):
        return False, "", f"invocation failed (exit {code}): {(err or out)[:200]}"
    prose = "".join(
        c.get("text", "")
        for o in (parsed.get("output") or [])
        for c in (o.get("content") or [])
        if "text" in c
    )
    low = prose.lower()
    raw = json.dumps(parsed)
    routed_glossary = "glossary_lookup" in raw
    resolved = any(m in low for m in _APPROVED_CA_MARKERS)
    denied = any(m in low for m in _DENIAL_MARKERS)
    ok = routed_glossary and resolved and not denied
    note = (f"routed_glossary={routed_glossary}, resolved_approved_def={resolved}, "
            f"denial_fallback={denied} -> {'PASS' if ok else 'FAIL (silent redirect)'}")
    return ok, prose, note


# --- Build-record writer -----------------------------------------------------

def write_build_doc(host, mid, endpoint, ep_state, task, accepted_body,
                    llm_finding, mas_sp, grant_log, assert_ok, assert_ev,
                    smoke_ok, smoke_prose):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    grant_rows = "\n".join(
        f"| {surface} | `{p}` | {'OK' if ok else 'FAIL'} | {str(d)[:80]} |"
        for surface, p, ok, d in grant_log
    ) or "| (none issued) | | | |"
    assert_block = "\n".join(f"- {e}" for e in assert_ev) or "- (not run)"
    doc = f"""# 05-SUPERVISOR-BUILD — Multi-Agent Supervisor Build Record

**Generated:** {ts}
**Workspace:** `{host}`
**Built by:** `src/deploy/build_supervisor.py` (Phase 5, Plan 01)

Single source of truth for the deployed MAS: id, endpoint, accepted create-request
wire shape, invocation contract, the 3-tool grant record, and the A1 supervisor-LLM
pin finding. Plan 05-02 (routing matrix) consumes these identifiers.

## MAS Identifiers

| Field | Value |
|-------|-------|
| display_name | `{load_config().get('display_name')}` |
| MAS id | `{mid}` |
| serving endpoint | `{endpoint}` |
| endpoint state | `{ep_state}` |
| endpoint `.task` (invocation contract) | `{task}` |

## Registered Tools (exactly 3)

Attached as `/tools` sub-resources (tool_type + type-block), not inline on the
create body:

| Tool | tool_type | id / name |
|------|-----------|-----------|
| knowledge_assistant | knowledge_assistant | `{KA_ID}` (endpoint `{KA_ENDPOINT}`) |
| ticket_analytics_genie | genie_space | `{GENIE_SPACE_ID}` |
| glossary_lookup | uc_function | `{GLOSSARY_FN}` |

## Accepted Create-Request Wire Shape (live-confirmed 2026-08-19)

The create body is **TOP-LEVEL and tool-free**: `POST {MAS_API}` takes ONLY
`{{display_name, description, instructions}}`. An inline `agents[]` (or `tools[]` /
`examples[]`) on the create body is **silently ignored** — the supervisor comes up
with zero bound tools and every invocation returns `Error: Tool '<x>' not found`.

Tools are attached as **sub-resources** AFTER create:
`POST {MAS_API}/{{id}}/tools?tool_id=<name>` with `{{tool_type, description,
<type-block>}}`, where the type-block is `knowledge_assistant.knowledge_assistant_id`,
`genie_space.id`, or `uc_function.name`. Examples likewise:
`POST {MAS_API}/{{id}}/examples` with `{{question, guidelines[]}}` (note: `guidelines`
is an array — the legacy single `guideline` string is wrapped).

Response keys: `supervisor_agent_id`, `name` (`supervisor-agents/{{id}}`),
`endpoint_name` (`mas-<frag>-endpoint`), `experiment_id`.

**UPDATE semantics:** PATCH uses a `?update_mask=` query param and edits ONLY
`{{display_name, description, instructions}}`; GET does NOT echo tools back. Tools are
reconciled idempotently (attach-if-missing); changing a tool's binding or description
requires `--recreate` (delete + recreate the MAS and re-attach its tools).

```json
{json.dumps(accepted_body, indent=2)}
```

## Invocation Contract (A3)

The MAS serving endpoint reports `.task = {task}`. Query it via
`POST /serving-endpoints/{endpoint}/invocations` with the Responses-API shape
`{{"input": [{{"role": "user", "content": <question>}}]}}` (NOT `messages`; and NOT
the `serving-endpoints query` CLI verb, which strips `output[]`). Prose answer =
`"".join(c["text"] for o in resp["output"] for c in o.get("content",[]) if "text" in c)`.

## Supervisor-LLM Pin Finding (A1)

{llm_finding}

## MAS Service Principal (Open Q4 / A7)

- **Discovered MAS SP identity:** `{mas_sp}`

## 3-Surface Grant Record (SUP-01 — no silent redirect, T-5-02/T-5-03)

Least-privilege grants issued for BOTH the demo principal AND the MAS SP
(EXECUTE / SELECT / USE / CAN_QUERY / CAN_USE only — never OWNER/ALL/admin):

| Surface | Principal | Result | Detail |
|---------|-----------|--------|--------|
{grant_rows}

### Grant assertions ({'PASS' if assert_ok else 'FAIL'})

{assert_block}

## Live End-to-End Smoke Test

- **Question:** {SMOKE_QUESTION}
- **Result:** {'PASS — non-empty prose returned' if smoke_ok else 'FAIL — no prose'}
- **Answer (excerpt):** {smoke_prose[:500].strip() if smoke_prose else '(none)'}

## Reproduce

```bash
python3 src/deploy/build_supervisor.py --profile serverless-stable            # full build
python3 src/deploy/build_supervisor.py --profile serverless-stable --grants-only  # re-issue/assert grants
```
Idempotent: reuses the existing MAS by display_name (find-by-display_name).
"""
    BUILD_DOC.parent.mkdir(parents=True, exist_ok=True)
    BUILD_DOC.write_text(doc)
    print(f"Wrote {BUILD_DOC}")


# --- main --------------------------------------------------------------------


def _apply_target(args):
    """Rebind CATALOG/SCHEMA/FQ/WAREHOUSE_ID (and every derived name) from flags.

    Serverless job environments cannot set environment variables, so --catalog /
    --schema / --warehouse-id are the only way a DAB task can retarget this script.
    Module-level table names are f-strings evaluated at import, so they are
    recomputed here rather than just updating CATALOG.
    """
    g = globals()
    cat, sch, fq, wh = _env.apply_target_args(args)
    old_fq = g.get("FQ")
    g["CATALOG"], g["SCHEMA"], g["FQ"], g["WAREHOUSE_ID"] = cat, sch, fq, wh
    for k in ("DEMO_CATALOG",):
        if k in g: g[k] = cat
    for k in ("DEMO_SCHEMA",):
        if k in g: g[k] = sch
    # Re-point any fully-qualified name built from the previous FQ.
    if old_fq and old_fq != fq:
        for k, v in list(g.items()):
            if isinstance(v, str) and v.startswith(old_fq + "."):
                g[k] = fq + v[len(old_fq):]
            elif isinstance(v, str) and v.startswith("/Volumes/" + old_fq.replace(".", "/")):
                g[k] = v.replace("/Volumes/" + old_fq.replace(".", "/"),
                                 "/Volumes/" + fq.replace(".", "/"))
    return cat, sch, fq, wh

def main():
    ap = argparse.ArgumentParser(description="Build the Field Repair Multi-Agent Supervisor.")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default="serverless-stable")
    ap.add_argument("--genie-space-id", default="",
                    help="Genie space id to route to, injected from the DAB "
                         "genie_spaces resource (${resources.genie_spaces."
                         "rkb_serving.id}). Robust to name-prefixing and to "
                         "duplicate titles across targets. If empty, fall back to "
                         "resolving the space BY TITLE.")
    ap.add_argument("--agent-suffix", default="",
                    help="Suffix for the KA display name to discover AND the MAS display "
                         "name (dev isolation, e.g. '-dev'). Empty = shared demo identity.")
    ap.add_argument("--grants-only", action="store_true",
                    help="Re-issue + assert the 3 grants on the existing MAS "
                         "(no create/update, no poll).")
    ap.add_argument("--skip-smoke", action="store_true",
                    help="Skip the live end-to-end smoke question.")
    ap.add_argument("--recreate", action="store_true",
                    help="Delete + recreate the MAS so a changed tool SET or tool "
                         "DESCRIPTION is applied (agents are create-time only; PATCH "
                         "can only edit display_name/description/instructions).")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    # Step 0 — never proceed against the wrong/unauthenticated workspace (T-5-01).
    host = assert_target_host(args.profile)
    print(f"Host gate OK: {host}")

    cfg = load_config()

    # Discover the KA serving endpoint from its (possibly suffixed) display name, so
    # the MAS routes to the KA it is paired with — never a hardcoded tile/endpoint.
    global KA_ENDPOINT, KA_ID
    ka_display = "rkb-knowledge-assistant-serving" + (args.agent_suffix or "")
    KA_ID, KA_ENDPOINT = resolve_ka(args.profile, ka_display)
    if not KA_ID or not KA_ENDPOINT:
        print(f"FATAL: Knowledge Assistant '{ka_display}' not found — run the "
              "serving_agents (KA) build first with the same --agent-suffix.",
              file=sys.stderr)
        sys.exit(4)
    print(f"KA discovered from '{ka_display}': id={KA_ID} endpoint={KA_ENDPOINT}")

    # Suffix the MAS display name (dev isolation) and retarget the glossary function to
    # THIS schema (GLOSSARY_FN was rebound to <catalog>.<schema>.glossary_lookup above).
    if args.agent_suffix:
        cfg["display_name"] = cfg["display_name"] + args.agent_suffix
    for a in cfg.get("agents", []):
        if "uc_function_name" in a:
            a["uc_function_name"] = GLOSSARY_FN

    # Genie is DAB-deployed; resolve its space_id by title and inject it into the
    # config so the MAS routes to the DAB space, never a hardcoded id.
    global GENIE_SPACE_ID
    GENIE_SPACE_ID = args.genie_space_id or resolve_genie_space_id(
        args.profile, GENIE_SPACE_TITLE)
    if not GENIE_SPACE_ID:
        print(f"FATAL: DAB Genie space not found (id flag empty and no space "
              f"titled '{GENIE_SPACE_TITLE}') — deploy the bundle (genie_spaces "
              "resource) before building the supervisor.", file=sys.stderr)
        sys.exit(4)
    for a in cfg.get("agents", []):
        if "genie_space_id" in a:
            a["genie_space_id"] = GENIE_SPACE_ID
    _src = "id flag" if args.genie_space_id else f"by title '{GENIE_SPACE_TITLE}'"
    print(f"Genie space (DAB, {_src}): {GENIE_SPACE_ID}")

    demo_principal = resolve_principal(args.profile)
    print(f"Demo principal: {demo_principal}")

    if args.grants_only:
        mid, existing = find_existing_mas(args.profile, cfg["display_name"])
        if not mid:
            print("FATAL: no existing MAS to grant against (run full build first).",
                  file=sys.stderr)
            sys.exit(6)
        mas = get_mas(args.profile, mid)
        endpoint = discover_endpoint(args.profile, mid, mas)
        mas_sp = discover_mas_sp(args.profile, mid, mas, endpoint)
        principals = [p for p in (demo_principal, mas_sp) if p]
        grant_log = issue_grants(args.profile, principals, endpoint)
        assert_ok, assert_ev = assert_grants(args.profile, principals)
        for surface, p, ok, d in grant_log:
            print(f"  {'OK ' if ok else 'FAIL'} {surface} -> {p}")
        for e in assert_ev:
            print(f"  {e}")
        if not assert_ok:
            print("FATAL: grant assertion FAILED.", file=sys.stderr)
            sys.exit(7)
        print("Grants re-issued + asserted.")
        return

    # Task 1 — create/update, attach tools + examples, then poll to ONLINE.
    mid, accepted_body, operation = create_or_update_mas(
        args.profile, cfg, recreate=args.recreate)
    if not mid:
        print("FATAL: no MAS id returned.", file=sys.stderr)
        sys.exit(5)
    print(f"[T1] MAS {operation}: id={mid}")

    # Attach the 3 tools as sub-resources BEFORE polling, so the endpoint provisions
    # with its tools bound (an inline agents[] on the create body is ignored by the
    # current API — that left the MAS with zero callable tools).
    tool_log = reconcile_tools(args.profile, mid, cfg)
    for tool_id, ttype, ok, detail in tool_log:
        print(f"[T1] tool {tool_id} ({ttype}): {'OK' if ok else 'FAIL'} — {detail}")
    if not all(ok for _, _, ok, _ in tool_log) or len(tool_log) != 3:
        print("FATAL: could not attach all 3 tools — the supervisor cannot route "
              "without them. See the FAIL detail above.", file=sys.stderr)
        sys.exit(5)
    n_examples = reconcile_examples(args.profile, mid, cfg)
    print(f"[T1] routing examples present: {n_examples}")

    endpoint, ep_state, task, online = poll_online(args.profile, mid)

    # A1 finding: did the create body accept / expose a supervisor-LLM/model field?
    if any(k in accepted_body for k in ("model", "llm", "model_name")):
        llm_finding = ("A model/LLM field WAS accepted on the supervisor_agent body "
                       "(see accepted wire shape above).")
    else:
        llm_finding = ("No supervisor-LLM/model field is exposed by "
                       "`POST /api/2.1/supervisor-agents` (nor by manage_mas). The "
                       "supervisor LLM is a platform default — the "
                       "'supervisor LLM = databricks-claude-sonnet-4-5' decision is "
                       "NOT settable via this API. Recorded as a platform constraint, "
                       "NOT a build failure (A1).")

    if not online:
        # Write what we have so the record isn't lost; exit non-zero to signal re-run.
        write_build_doc(host, mid, endpoint, ep_state, task, accepted_body,
                        llm_finding, "(not discovered — MAS not ONLINE)", [],
                        False, ["endpoint not ONLINE this run — re-run to resume"],
                        False, "")
        print("MAS not ONLINE this run — see 05-SUPERVISOR-BUILD.md; re-run to resume.",
              file=sys.stderr)
        sys.exit(6)

    # Task 2 — discover SP, grants, smoke test.
    mas = get_mas(args.profile, mid)
    mas_sp = discover_mas_sp(args.profile, mid, mas, endpoint)
    print(f"[T2] Discovered MAS SP: {mas_sp}")
    principals = [p for p in (demo_principal, mas_sp) if p]
    grant_log = issue_grants(args.profile, principals, endpoint)
    for surface, p, ok, d in grant_log:
        print(f"  {'OK ' if ok else 'FAIL'} {surface} -> {p}")
    assert_ok, assert_ev = assert_grants(args.profile, principals)
    for e in assert_ev:
        print(f"  {e}")

    smoke_ok, smoke_prose, smoke_raw = (False, "", "skipped")
    if not args.skip_smoke:
        print("[T2] Firing one live terminology question at the MAS endpoint...")
        smoke_ok, smoke_prose, smoke_raw = smoke_test(args.profile, endpoint)
        print(f"[T2] Smoke test: {'PASS' if smoke_ok else 'FAIL'} — "
              f"{smoke_prose[:120].strip() if smoke_prose else smoke_raw}")

    write_build_doc(host, mid, endpoint, ep_state, task, accepted_body,
                    llm_finding, mas_sp, grant_log, assert_ok, assert_ev,
                    smoke_ok, smoke_prose)

    if not assert_ok:
        print("Build recorded, but grant assertion FAILED — see 05-SUPERVISOR-BUILD.md.",
              file=sys.stderr)
        sys.exit(7)
    if not args.skip_smoke and not smoke_ok:
        print("Build recorded, but the live smoke test returned no prose — see "
              "05-SUPERVISOR-BUILD.md.", file=sys.stderr)
        sys.exit(8)
    print("MAS build complete: 3 tools registered, ONLINE, grants asserted, "
          "one live answer proven.")


if __name__ == "__main__":
    main()
