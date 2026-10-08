#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant — Environment Preflight harness.

Re-runnable proof that the target Databricks workspace
(the reference workspace) can support the entire
Agent Bricks build. Produces reports/01-PREFLIGHT-REPORT.md
with a per-criterion PASS/FAIL/BLOCKED evidence table.

Design (per Phase 1 CONTEXT + RESEARCH):
  - Step 0 host-assertion gate: refuses to run any check unless the resolved
    Databricks workspace host is the target. Prevents silently running against
    the wrong workspace (the DEFAULT/e2-demo-field-eng mistake).
  - Structured-verdict pattern: every check returns
    {criterion, status: PASS|FAIL|BLOCKED, evidence, escalation}.
  - Honest reporting: a missing enablement is BLOCKED with an explicit
    escalation ask — never a faked PASS.
  - Doubles as demo-day endpoint warm-up (reused in Phase 8, DEMO-02).

Checks use the Databricks CLI (`databricks ... --profile serverless-stable`) as
the portable engine. The Databricks Agent Skills MCP tools (manage_workspace, manage_ka,
manage_mas, manage_serving_endpoint, execute_sql) are the interactive equivalent
and were used to author/verify these checks; this script is the re-runnable form.

Usage:
    python preflight/preflight.py            # run all checks, regenerate report
    python preflight/preflight.py --profile serverless-stable
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Host safety gate. Its job is to refuse to build into the WRONG workspace, which
# matters because these scripts create catalogs, agents and apps.
#
# In this template it is CONFIGURABLE rather than pinned: set RKB_TARGET_HOST to a
# fragment of your own workspace hostname. Leave it unset and the gate is DISABLED
# (any authenticated workspace is accepted) — convenient for a first run, but set it
# before you point this at anything you care about.
TARGET_HOST_FRAGMENT = os.environ.get("RKB_TARGET_HOST", "")
TARGET_WORKSPACE_ID = os.environ.get("RKB_TARGET_WORKSPACE_ID", "")
DEFAULT_PROFILE = os.environ.get("RKB_PROFILE", "DEFAULT")

# Demo location. Proposed rkb_demo.knowledge_agent required CREATE CATALOG on the
# metastore, which the demo principal lacks; per CONTEXT D-03 ("adjust to workspace
# UC conventions") we use a dedicated schema in the existing managed catalog instead.
DEMO_CATALOG = os.environ.get("RKB_CATALOG", "main")
DEMO_SCHEMA = os.environ.get("RKB_SCHEMA", "troubleshooting_knowledge_agent")

FM_MODELS = ["databricks-claude-sonnet-4-5", "databricks-claude-haiku-4-5"]
# databricks-claude-sonnet-4 (no suffix) is DEPRECATED — never query it.

# Agent Bricks / KA + FM APIs supported without cross-geo routing in these region prefixes.
SUPPORTED_REGION_PREFIXES = ("us-", "eu-", "ca-", "sa-")
APAC_CROSS_GEO_PREFIXES = ("ap-",)

REPORT_PATH = Path


def run_cli(args, profile):
    """Run a databricks CLI command, return (exit_code, stdout, stderr)."""
    cmd = ["databricks", *args, "--profile", profile]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "databricks CLI not found"


# --- Auth: SDK WorkspaceClient (works BOTH locally and inside DAB jobs) -------
# The CLI (`databricks --profile <name>`) cannot authenticate on serverless job
# compute — there is no working named profile there. The SDK's WorkspaceClient
# authenticates from AMBIENT credentials in-job and from the chosen CLI profile
# locally, so routing SQL/warehouse/identity through it makes these scripts run
# unchanged in both places.
_WC_CACHE = {}


def _on_databricks():
    """True when running on Databricks compute (job/notebook) vs a local shell."""
    return bool(
        os.environ.get("DATABRICKS_RUNTIME_VERSION")
        or os.environ.get("DB_IS_DRIVER")
        or os.environ.get("SPARK_LOCAL_DIRS")
    )


def workspace_client(profile=None):
    """Return a cached WorkspaceClient: ambient in-job, the CLI profile locally."""
    key = profile or "__ambient__"
    if key not in _WC_CACHE:
        from databricks.sdk import WorkspaceClient
        if _on_databricks() or not profile or profile == DEFAULT_PROFILE:
            _WC_CACHE[key] = WorkspaceClient()            # ambient (in-job / env)
        else:
            _WC_CACHE[key] = WorkspaceClient(profile=profile)  # local CLI profile
    return _WC_CACHE[key]


def api_do(method, path, profile, body=None, timeout=None):
    """Perform an arbitrary workspace REST call via the SDK's ApiClient.

    Drop-in replacement for `databricks api <method> <path> [--json <body>]` that works
    on serverless job compute (ambient auth). Returns (exit_code, stdout_json_str,
    stderr) to match the run_cli contract callers already parse.
    """
    try:
        res = workspace_client(profile).api_client.do(method.upper(), path, body=body)
        return 0, (json.dumps(res) if res is not None else ""), ""
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def verdict(criterion, status, evidence, escalation=""):
    return {
        "criterion": criterion,
        "status": status,
        "evidence": evidence,
        "escalation": escalation,
    }


# --- Step 0: host-assertion gate -------------------------------------------

def assert_target_host(profile):
    """HARD GATE. Returns resolved host or exits non-zero if not the target workspace."""
    try:
        host = workspace_client(profile).config.host or ""
    except Exception as e:  # noqa: BLE001
        print(f"FATAL: could not authenticate (profile '{profile}'): {e}. "
              "Locally: databricks auth login --host "
              f"https://{TARGET_HOST_FRAGMENT or '<workspace>'}.cloud.databricks.com "
              f"--profile {profile}", file=sys.stderr)
        sys.exit(2)
    if TARGET_HOST_FRAGMENT and TARGET_HOST_FRAGMENT not in host:
        print(f"FATAL: resolved host '{host}' is not the target "
              f"({TARGET_HOST_FRAGMENT}). Refusing to run against the wrong workspace.",
              file=sys.stderr)
        sys.exit(3)
    return host


def resolve_principal(profile):
    try:
        return workspace_client(profile).current_user.me().user_name or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def run_sql(statement, profile, warehouse_id):
    """Run a SQL statement on the serverless warehouse; return (state, data_array).

    Uses the SDK Statement Execution API (ambient auth in-job), polling to a terminal
    state so statements longer than the initial wait still resolve.
    """
    import time as _time
    from databricks.sdk.service.sql import StatementState
    w = workspace_client(profile)
    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=statement, wait_timeout="30s")
        sid = resp.statement_id
        deadline = _time.time() + 600
        while (sid and resp.status and resp.status.state in
               (StatementState.PENDING, StatementState.RUNNING)
               and _time.time() < deadline):
            _time.sleep(3)
            resp = w.statement_execution.get_statement(sid)
    except Exception:  # noqa: BLE001
        return "CLI_ERROR", None
    state = resp.status.state.value if resp.status and resp.status.state else "UNKNOWN"
    data = resp.result.data_array if resp.result else None
    return state, data


def first_warehouse_id(profile):
    try:
        whs = list(workspace_client(profile).warehouses.list())
    except Exception:  # noqa: BLE001
        return None
    if not whs:
        return None
    for wh in whs:
        if getattr(wh, "enable_serverless_compute", False):
            return wh.id
    return whs[0].id


def region_of(profile):
    code, out, _ = run_cli(["api", "get", "/api/2.1/unity-catalog/metastore_summary"], profile)
    if code != 0:
        return None
    try:
        return json.loads(out).get("region")
    except json.JSONDecodeError:
        return None


# --- ENV-01: region + serverless + UC + Agent Bricks surface ----------------

def check_env01(profile, warehouse_id):
    region = region_of(profile) or "unknown"
    region_ok = region.startswith(SUPPORTED_REGION_PREFIXES)
    apac = region.startswith(APAC_CROSS_GEO_PREFIXES)
    # serverless compute
    sql_state, _ = (run_sql("SELECT 1", profile, warehouse_id) if warehouse_id
                    else ("NO_WAREHOUSE", None))
    serverless_ok = sql_state == "SUCCEEDED"
    # UC
    uc_code, _, _ = run_cli(["catalogs", "list"], profile)
    uc_ok = uc_code == 0
    # Agent Bricks tile surface
    tiles_code, _, _ = run_cli(["api", "get", "/api/2.0/tiles"], profile)
    tiles_ok = tiles_code == 0

    if region_ok and serverless_ok and uc_ok and tiles_ok:
        status = "PASS"
    else:
        status = "BLOCKED"
    ev = (f"region={region} ({'supported' if region_ok else 'APAC cross-geo' if apac else 'UNSUPPORTED'}); "
          f"serverless SQL={'ok' if serverless_ok else sql_state}; "
          f"UC catalogs list={'ok' if uc_ok else 'FAIL'}; "
          f"Agent Bricks /api/2.0/tiles={'200' if tiles_ok else 'FAIL'}")
    esc = "" if status == "PASS" else "Enable serverless/UC or use a supported region for Agent Bricks."
    return verdict("ENV-01 KA available (region + serverless + UC)", status, ev, esc)


# --- ENV-02: MAS preview (human-verified, recorded) -------------------------

def check_env02(profile):
    """MAS preview toggle is account-admin-only (no read API). Read the recorded
    human verification; corroborate with the Agent Bricks tiles API."""
    tiles_code, _, _ = run_cli(["api", "get", "/api/2.0/tiles"], profile)
    tiles_ok = tiles_code == 0
    hv_path = Path("preflight/env02_human_verification.json")
    if hv_path.exists():
        try:
            hv = json.loads(hv_path.read_text())
        except json.JSONDecodeError:
            hv = {}
        if hv.get("mas_preview_enabled") is True:
            ev = (f"Account-admin confirmed MAS preview ON ({hv.get('verified_by','?')}, "
                  f"{hv.get('verified_on','?')}); tiles API={'200' if tiles_ok else 'FAIL'}")
            return verdict("ENV-02 Multi-Agent Supervisor preview enabled", "PASS", ev, "")
        if hv.get("mas_preview_enabled") is False:
            return verdict("ENV-02 Multi-Agent Supervisor preview enabled", "BLOCKED",
                           "Account-admin reported MAS preview OFF/absent",
                           "Account admin: Account Console () → Previews → enable 'Multi-Agent Supervisor'")
    return verdict("ENV-02 Multi-Agent Supervisor preview enabled", "AWAITING HUMAN",
                   f"tiles API reachable={'200' if tiles_ok else 'FAIL'}; no recorded human verification",
                   "Account admin: confirm 'Multi-Agent Supervisor' toggle on the account Previews page, then record it in preflight/env02_human_verification.json")


# --- ENV-03: demo schema exists + writable grants ---------------------------

def check_env03(profile, warehouse_id):
    if not warehouse_id:
        return verdict("ENV-03 demo catalog/schema + grants", "BLOCKED",
                       "no serverless warehouse to probe with", "Provision a serverless SQL warehouse.")
    fq = f"{DEMO_CATALOG}.{DEMO_SCHEMA}"
    probe = f"{fq}.__preflight_probe"
    steps = [
        ("create schema", f"CREATE SCHEMA IF NOT EXISTS {fq}"),
        ("create table", f"CREATE TABLE IF NOT EXISTS {probe} (x INT)"),
        ("insert", f"INSERT INTO {probe} VALUES (1)"),
        ("select", f"SELECT COUNT(*) FROM {probe}"),
        ("drop", f"DROP TABLE IF EXISTS {probe}"),
    ]
    for label, stmt in steps:
        state, _ = run_sql(stmt, profile, warehouse_id)
        if state != "SUCCEEDED":
            return verdict("ENV-03 demo catalog/schema + grants", "BLOCKED",
                           f"{label} failed ({state}) on {fq}",
                           f"Grant CREATE/MODIFY/SELECT on a schema to the demo principal.")
    return verdict("ENV-03 demo catalog/schema + grants", "PASS",
                   f"{fq}: round-trip CREATE TABLE→INSERT→SELECT→DROP succeeded (grants sufficient for KA/Genie assets)",
                   "")


# --- ENV-04: live FM completions -------------------------------------------

def check_fm_models(profile):
    results = []
    for model in FM_MODELS:
        payload = json.dumps({
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
            "max_tokens": 16,
        })
        code, out, err = run_cli(
            ["serving-endpoints", "query", model, "--json", payload], profile
        )
        content = ""
        if code == 0:
            try:
                data = json.loads(out)
                choices = data.get("choices") or []
                if choices:
                    content = (choices[0].get("message") or {}).get("content", "")
            except json.JSONDecodeError:
                pass
        if content.strip():
            results.append((model, "PASS", f"live completion: {content.strip()[:40]!r}"))
        else:
            results.append((model, "BLOCKED",
                            f"no completion (exit {code}: {(err or out)[:80]})"))
    statuses = {s for _, s, _ in results}
    status = "PASS" if statuses == {"PASS"} else "BLOCKED"
    evidence = "; ".join(f"{m}={s}" for m, s, _ in results)
    esc = ("" if status == "PASS"
           else "Enable / grant query access to the FM API endpoint(s) above in the "
                "workspace serving UI or via account admin.")
    return verdict("ENV-04 FM live completions", status, evidence, esc)


def build_report(host, principal, verdicts):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Phase 1 — Environment Preflight Report",
        "",
        f"**Workspace:** `{host}` (id `{TARGET_WORKSPACE_ID}`)",
        f"**Demo principal:** {principal}",
        f"**Generated:** {ts}",
        "",
        "Honest reporting: a non-PASS is BLOCKED with an explicit escalation ask — never a faked PASS.",
        "",
        "| Criterion | Status | Evidence | Escalation |",
        "|-----------|--------|----------|------------|",
    ]
    for v in verdicts:
        lines.append(
            f"| {v['criterion']} | {v['status']} | {v['evidence']} | {v['escalation'] or '—'} |"
        )
    lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    args = ap.parse_args()

    # Step 0 — never proceed against the wrong/unauthenticated workspace.
    host = assert_target_host(args.profile)
    principal = resolve_principal(args.profile)

    warehouse_id = first_warehouse_id(args.profile)

    verdicts = [
        check_env01(args.profile, warehouse_id),
        check_env02(args.profile),
        check_env03(args.profile, warehouse_id),
        check_fm_models(args.profile),
    ]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(build_report(host, principal, verdicts))
    print(f"Wrote {REPORT_PATH}")
    for v in verdicts:
        print(f"  {v['status']:8} {v['criterion']}")


if __name__ == "__main__":
    main()
