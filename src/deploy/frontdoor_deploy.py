#!/usr/bin/env python3
"""
Host-asserted deploy driver for the Field Repair front-door Databricks App (Phase 06, Plan 05).

Deploys the FastAPI + React front door (frontdoor/) as a Databricks App, binds the
`serving-endpoint` resource → the warm MAS `the MAS endpoint` (CAN_QUERY, auto-granted
to the app SP on deploy), and enables OBO user authorization with the confirmed serving
scope (`serving.serving-endpoints`, 06-PREFLIGHT). The end user's `x-forwarded-access-token`
is what actually invokes the MAS (server-side, 06-03) — so each user needs their own
CAN_QUERY on the endpoint (RESEARCH Pitfall 3).

Deploy sequence
---------------
  1. Assert the target host BEFORE any workspace write (CLAUDE.md platform constraint).
  2. Create the app if it does not exist (idempotent; reuse `rkb-frontdoor` by name).
  3. Stage a clean upload tree: backend + app.yaml + requirements.txt + frontend/dist ONLY
     (EXCLUDE node_modules / .venv / frontend/src / tests / __pycache__ — 4-deployment.md).
  4. Import the staged tree into the workspace source path and `databricks apps deploy`.
  5. Bind the `serving-endpoint` resource (CAN_QUERY) + set `user_api_scopes` via
     `databricks apps create-update <APP> --json @update.json` — READ the app's current
     resources first and MERGE (update_mask replaces the listed field wholesale — Pitfall 6).
  6. Redeploy so the valueFrom + scopes take effect.
  7. Verify via `databricks apps get`: state RUNNING, serving-endpoint bound, serving scope set.

Guardrails
----------
  * NEVER `--recreate` the MAS (resets the Phase-5 per-tile SSP authorization — Pitfall 4).
    This driver references `the MAS endpoint` by NAME only.
  * NEVER prints tokens. Resources/scopes are logged by name only.
  * `--dry-run` prints the planned actions and exits 0 WITHOUT mutating the workspace.
  * `--print-url` prints ONLY the running app URL to stdout (for the smoke-gate pipeline).

Usage
-----
    python src/deploy/frontdoor_deploy.py --profile serverless-stable --dry-run
    python src/deploy/frontdoor_deploy.py --profile serverless-stable
    python src/deploy/frontdoor_deploy.py --profile serverless-stable --print-url
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# --- Constants (CLAUDE.md platform constraint + 06-PREFLIGHT confirmed values) ---
TARGET_HOST = os.environ.get("RKB_TARGET_HOST", "")  # optional host guard (soft)
APP_NAME = "rkb-frontdoor"
MAS_ENDPOINT_NAME = os.environ.get("MAS_ENDPOINT_NAME", "")  # set via --mas-endpoint-name
SERVING_RESOURCE_KEY = "serving-endpoint"            # matches app.yaml valueFrom
SERVING_PERMISSION = "CAN_QUERY"                     # least privilege (T-06-12)
SERVING_SCOPE = "serving.serving-endpoints"          # 06-PREFLIGHT confirmed (HIGH)

# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD. The app SOURCE tree lives in frontdoor/ (repo
# root), not next to this script under src/deploy/, so HERE points two levels up.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
HERE = os.path.normpath(os.path.join(_SCRIPT_DIR, "..", "..", "frontdoor"))
# preflight lives next to this script; put it on the path (in-job CWD is this dir, but
# be explicit so a local run from the repo root also resolves it) for SDK auth helpers.
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)
import preflight as _pf  # noqa: E402  (SDK WorkspaceClient / api_do: ambient in-job)

# Files/dirs shipped to the app. EVERYTHING else is excluded from the upload tree.
INCLUDE_FILES = ["app.py", "app.yaml", "requirements.txt"]
INCLUDE_DIRS = ["server"]                            # backend package
INCLUDE_FRONTEND_DIST = os.path.join("frontend", "dist")  # built SPA only
EXCLUDE_ALWAYS = {"node_modules", ".venv", "__pycache__", "tests", "src", ".git"}


def _run(cmd, capture=True, check=True):
    """Run a CLI command. Returns CompletedProcess. Never echoes token material."""
    result = subprocess.run(
        cmd, capture_output=capture, text=True, check=False
    )
    if check and result.returncode != 0:
        sys.stderr.write(
            f"FAIL: command exited {result.returncode}: {' '.join(cmd)}\n"
            f"{(result.stderr or '')[:1000]}\n"
        )
        sys.exit(result.returncode)
    return result


def assert_target_host(profile):
    """Resolve the workspace host via the SDK (ambient in-job, CLI profile locally).

    TARGET_HOST is enforced as a soft guard: warn (don't hard-fail) if it differs, so
    the template is portable to other workspaces while still flagging a surprise.
    """
    try:
        host = _pf.workspace_client(profile).config.host or ""
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"FATAL: could not authenticate (profile '{profile}'): {e}\n")
        sys.exit(3)
    bare = host.replace("https://", "").replace("http://", "").rstrip("/")
    if TARGET_HOST and bare != TARGET_HOST:
        sys.stderr.write(
            f"WARN: workspace host is '{bare}', not the reference '{TARGET_HOST}'.\n")
    return bare


def app_get(profile):
    """Return the app record dict, or None if the app does not exist (SDK apps API)."""
    try:
        return _pf.workspace_client(profile).apps.get(APP_NAME).as_dict()
    except Exception:  # noqa: BLE001  (NOT_FOUND etc.)
        return None


def print_url(profile):
    """Print ONLY the running app URL to stdout (for the smoke-gate pipeline)."""
    app = app_get(profile)
    if not app or not app.get("url"):
        sys.stderr.write(f"FAIL: app '{APP_NAME}' has no URL (not deployed?).\n")
        sys.exit(4)
    print(app["url"])
    # `return`, not sys.exit(0): a success SystemExit is reported as a task failure
    # by serverless job compute (kernel exec wrapper).
    return


def stage_tree():
    """Copy the clean upload tree to a temp dir. Ships backend + app.yaml +
    requirements.txt + frontend/dist ONLY. Returns the staging path."""
    staging = tempfile.mkdtemp(prefix="rkb-frontdoor-deploy-")
    for f in INCLUDE_FILES:
        src = os.path.join(HERE, f)
        if not os.path.exists(src):
            sys.stderr.write(f"FAIL: required file missing: {f}\n")
            sys.exit(5)
        shutil.copy2(src, os.path.join(staging, f))
    for d in INCLUDE_DIRS:
        shutil.copytree(
            os.path.join(HERE, d), os.path.join(staging, d),
            ignore=shutil.ignore_patterns(*EXCLUDE_ALWAYS, "*.pyc"),
        )
    dist_src = os.path.join(HERE, INCLUDE_FRONTEND_DIST)
    if not os.path.isdir(dist_src):
        sys.stderr.write(
            "FAIL: frontend/dist missing — run `npm run build` in frontdoor/frontend "
            "first (06-04 committed it).\n"
        )
        sys.exit(5)
    dist_dst = os.path.join(staging, "frontend", "dist")
    os.makedirs(os.path.dirname(dist_dst), exist_ok=True)
    shutil.copytree(dist_src, dist_dst)
    return staging


def merged_resources(app):
    """Merge the serving-endpoint resource into the app's CURRENT resources.

    update_mask=resources replaces the entire array wholesale (Pitfall 6), so we
    READ the existing resources and MERGE our serving-endpoint entry, replacing any
    prior entry with the same key.
    """
    current = list((app or {}).get("resources") or [])
    kept = [r for r in current if r.get("name") != SERVING_RESOURCE_KEY]
    kept.append({
        "name": SERVING_RESOURCE_KEY,
        "description": "Warm Multi-Agent Supervisor endpoint (OBO CAN_QUERY)",
        "serving_endpoint": {
            "name": MAS_ENDPOINT_NAME,
            "permission": SERVING_PERMISSION,
        },
    })
    return kept


def create_update(profile, app, dry_run):
    """Bind the serving-endpoint resource (merged) + set user_api_scopes via create-update.

    Two separate masked updates so each field is scoped precisely:
      * update_mask=resources → MERGED resource array (never detaches others).
      * update_mask=user_api_scopes → the confirmed serving scope (re-applied every deploy).
    """
    resources = merged_resources(app)
    if dry_run:
        print(f"  [dry-run] apps.update {APP_NAME}: bind {SERVING_RESOURCE_KEY}→"
              f"{MAS_ENDPOINT_NAME} {SERVING_PERMISSION}; user_api_scopes [{SERVING_SCOPE}] "
              f"({len(resources)} total resources)")
        return
    # One SDK apps.update carrying the MERGED resources + the scope, so neither field
    # is dropped. (The DAB apps resource also declares the serving-endpoint binding;
    # this re-asserts it and adds user_api_scopes, which DAB cannot express.)
    from databricks.sdk.service.apps import App
    merged = App.from_dict({
        "name": APP_NAME,
        "resources": resources,
        "user_api_scopes": [SERVING_SCOPE],
    })
    print(f"  apps.update {APP_NAME}: {len(resources)} resource(s) + "
          f"user_api_scopes=[{SERVING_SCOPE}]")
    # Pass name/app as keywords: newer databricks-sdk makes `app` keyword-only
    # (update(self, name, *, app)), so the positional call raised "takes 2
    # positional arguments but 3 were given" on the job's SDK. Keywords work on
    # both the positional (older) and keyword-only (newer) signatures.
    _pf.workspace_client(profile).apps.update(name=APP_NAME, app=merged)


def deploy(profile, source_path):
    """databricks apps deploy (NOT bare bundle deploy — that leaves the app stopped)."""
    print(f"  apps deploy {APP_NAME} --source-code-path {source_path}")
    _run(["databricks", "apps", "deploy", APP_NAME,
          "--source-code-path", source_path,
          "--profile", profile, "--auto-approve"])


def import_tree(profile, staging, source_path):
    """Import the clean staged tree into the workspace source path (overwrite)."""
    print(f"  workspace import-dir {source_path} (clean staged tree)")
    _run(["databricks", "workspace", "import-dir", staging, source_path,
          "--overwrite", "--profile", profile])


def verify(profile):
    """Verify: state RUNNING, serving-endpoint resource bound, serving scope present."""
    app = app_get(profile)
    if not app:
        sys.stderr.write("FAIL: app not found after deploy.\n")
        sys.exit(6)
    state = (app.get("app_status") or {}).get("state") \
        or (app.get("compute_status") or {}).get("state")
    resources = app.get("resources") or []
    bound = next((r for r in resources if r.get("name") == SERVING_RESOURCE_KEY), None)
    scopes = app.get("user_api_scopes") or []
    ok = True
    if state not in ("RUNNING", "ACTIVE"):
        sys.stderr.write(f"FAIL: app state is '{state}', expected RUNNING/ACTIVE.\n")
        ok = False
    if not bound or bound.get("serving_endpoint", {}).get("name") != MAS_ENDPOINT_NAME:
        sys.stderr.write("FAIL: serving-endpoint resource not bound to the MAS.\n")
        ok = False
    elif bound.get("serving_endpoint", {}).get("permission") != SERVING_PERMISSION:
        sys.stderr.write("FAIL: serving-endpoint permission is not CAN_QUERY.\n")
        ok = False
    if SERVING_SCOPE not in scopes:
        sys.stderr.write(f"FAIL: user_api_scopes missing '{SERVING_SCOPE}'.\n")
        ok = False
    print("\n=== VERIFY ===")
    print(f"  app:        {app.get('name')}")
    print(f"  url:        {app.get('url')}")
    print(f"  sp:         {app.get('service_principal_name')}")
    print(f"  state:      {state}")
    print(f"  resource:   {SERVING_RESOURCE_KEY} → "
          f"{(bound or {}).get('serving_endpoint', {}).get('name')} "
          f"({(bound or {}).get('serving_endpoint', {}).get('permission')})")
    print(f"  scopes:     {scopes}")
    print(f"  verify:     {'PASS' if ok else 'FAIL'}")
    if not ok:
        sys.exit(7)
    return app


def run_deploy(profile, dry_run):
    """Bind the OBO scope + MAS serving-endpoint on the DAB-created app.

    DAB owns app creation + code upload (apps.frontdoor `source_code_path`) and start
    (`bundle run frontdoor`). The ONLY thing DAB cannot express is `user_api_scopes`
    (OBO), so this job just re-asserts the serving-endpoint binding and adds the scope.
    """
    bare = assert_target_host(profile)
    print(f"Target host: {bare}")
    print(f"App: {APP_NAME}  |  MAS endpoint (by name, NEVER --recreate): {MAS_ENDPOINT_NAME}")

    app = app_get(profile)
    if app is None:
        sys.stderr.write(
            f"FATAL: app '{APP_NAME}' not found. It is created by DAB — deploy it first:\n"
            f"  databricks bundle deploy --select apps.frontdoor "
            f"--var app_name={APP_NAME} --var mas_endpoint_name={MAS_ENDPOINT_NAME} ...\n"
            f"This job only binds the OBO scope DAB cannot express.\n")
        sys.exit(4)

    if dry_run:
        print("\n[dry-run] planned action:")
        create_update(profile, app, dry_run=True)
        print(f"  then verify RUNNING + resource bound + scope '{SERVING_SCOPE}'")
        print("\n[dry-run] no workspace mutations performed. exit 0.")
        return

    # Bind serving-endpoint resource (merged) + user_api_scopes via the SDK.
    create_update(profile, app, dry_run=False)
    verify(profile)


def _current_user(profile):
    r = _run(["databricks", "current-user", "me", "--profile", profile, "-o", "json"],
             check=False)
    try:
        return json.loads(r.stdout).get("userName", "unknown")
    except (ValueError, TypeError):
        return "unknown"


def main():
    global APP_NAME, MAS_ENDPOINT_NAME  # rebound below; declared first (read in default=)
    ap = argparse.ArgumentParser(description="Deploy the Field Repair front-door Databricks App.")
    ap.add_argument("--profile", default="DEFAULT",
                    help="CLI profile for LOCAL runs. As a serverless job task, leave it "
                         "as DEFAULT — the SDK uses ambient auth.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned actions and exit 0 without mutating the workspace.")
    ap.add_argument("--print-url", action="store_true", dest="print_url",
                    help="Print ONLY the running app URL to stdout and exit.")
    ap.add_argument("--app-name", default=APP_NAME,
                    help="App name to deploy/bind. Injected from ${var.app_name} so "
                         "the name matches the DAB apps resource (lets a dev target "
                         "use its own app instead of colliding with the shared one).")
    ap.add_argument("--mas-endpoint-name", default=MAS_ENDPOINT_NAME,
                    help="MAS serving endpoint to bind (OBO CAN_QUERY). Injected from "
                         "${var.mas_endpoint_name} — the endpoint the rkb_agents job "
                         "created — so this never binds a stale hardcoded endpoint.")
    args = ap.parse_args()

    # Rebind the module globals so every helper uses the requested app/endpoint.
    APP_NAME = args.app_name
    if args.mas_endpoint_name:
        MAS_ENDPOINT_NAME = args.mas_endpoint_name

    if args.print_url:
        # Host-assert even for the read so we never point at the wrong workspace.
        assert_target_host(args.profile)
        print_url(args.profile)
        return

    run_deploy(args.profile, args.dry_run)


if __name__ == "__main__":
    main()
