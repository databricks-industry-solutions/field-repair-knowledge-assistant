#!/usr/bin/env python3
"""
Host-asserted deploy driver for the FIS R&D front-door Databricks App (Phase 06, Plan 05).

Deploys the FastAPI + React front door (frontdoor/) as a Databricks App, binds the
`serving-endpoint` resource → the warm MAS `mas-f5fc28b0-endpoint` (CAN_QUERY, auto-granted
to the app SP on deploy), and enables OBO user authorization with the confirmed serving
scope (`serving.serving-endpoints`, 06-PREFLIGHT). The end user's `x-forwarded-access-token`
is what actually invokes the MAS (server-side, 06-03) — so each user needs their own
CAN_QUERY on the endpoint (RESEARCH Pitfall 3).

Deploy sequence
---------------
  1. Assert the target host BEFORE any workspace write (CLAUDE.md platform constraint).
  2. Create the app if it does not exist (idempotent; reuse `fis-rnd-frontdoor` by name).
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
    This driver references `mas-f5fc28b0-endpoint` by NAME only.
  * NEVER prints tokens. Resources/scopes are logged by name only.
  * `--dry-run` prints the planned actions and exits 0 WITHOUT mutating the workspace.
  * `--print-url` prints ONLY the running app URL to stdout (for the smoke-gate pipeline).

Usage
-----
    python frontdoor/deploy.py --profile serverless-stable --dry-run
    python frontdoor/deploy.py --profile serverless-stable
    python frontdoor/deploy.py --profile serverless-stable --print-url
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

# --- Constants (CLAUDE.md platform constraint + 06-PREFLIGHT confirmed values) ---
TARGET_HOST = "fevm-serverless-stable-l26d62.cloud.databricks.com"
APP_NAME = "fis-rnd-frontdoor"
MAS_ENDPOINT_NAME = "mas-f5fc28b0-endpoint"          # reuse by NAME — never --recreate
SERVING_RESOURCE_KEY = "serving-endpoint"            # matches app.yaml valueFrom
SERVING_PERMISSION = "CAN_QUERY"                     # least privilege (T-06-12)
SERVING_SCOPE = "serving.serving-endpoints"          # 06-PREFLIGHT confirmed (HIGH)

HERE = os.path.dirname(os.path.abspath(__file__))

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
    """Refuse any workspace write outside the sanctioned FIS l26d62 workspace.

    Resolves the profile host from ~/.databrickscfg (authoritative, non-deprecated).
    """
    import configparser
    c = configparser.ConfigParser()
    c.read(os.path.expanduser("~/.databrickscfg"))
    if not c.has_section(profile):
        sys.stderr.write(f"FATAL: profile '{profile}' not found in ~/.databrickscfg.\n")
        sys.exit(3)
    host = c[profile].get("host", "")
    bare = host.replace("https://", "").replace("http://", "").rstrip("/")
    if bare != TARGET_HOST:
        sys.stderr.write(
            f"FATAL: profile '{profile}' host is '{bare}' — expected the FIS workspace "
            f"'{TARGET_HOST}' (CLAUDE.md platform constraint). Refusing to deploy.\n"
        )
        sys.exit(3)
    return bare


def app_get(profile):
    """Return the app record dict, or None if the app does not exist."""
    r = _run(["databricks", "apps", "get", APP_NAME, "--profile", profile, "-o", "json"],
             check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (ValueError, TypeError):
        return None


def print_url(profile):
    """Print ONLY the running app URL to stdout (for the smoke-gate pipeline)."""
    app = app_get(profile)
    if not app or not app.get("url"):
        sys.stderr.write(f"FAIL: app '{APP_NAME}' has no URL (not deployed?).\n")
        sys.exit(4)
    print(app["url"])
    sys.exit(0)


def stage_tree():
    """Copy the clean upload tree to a temp dir. Ships backend + app.yaml +
    requirements.txt + frontend/dist ONLY. Returns the staging path."""
    staging = tempfile.mkdtemp(prefix="fis-frontdoor-deploy-")
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
    resource_body = {"update_mask": "resources", "app": {"resources": resources}}
    scope_body = {
        "update_mask": "user_api_scopes",
        "app": {"user_api_scopes": [SERVING_SCOPE]},
    }
    if dry_run:
        print(f"  [dry-run] create-update {APP_NAME} update_mask=resources "
              f"(merge {SERVING_RESOURCE_KEY}→{MAS_ENDPOINT_NAME} {SERVING_PERMISSION}; "
              f"{len(resources)} total resources)")
        print(f"  [dry-run] create-update {APP_NAME} update_mask=user_api_scopes "
              f"([{SERVING_SCOPE}])")
        return
    for label, body in (("resources", resource_body), ("user_api_scopes", scope_body)):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(body, fh)
            path = fh.name
        try:
            print(f"  create-update {APP_NAME} (update_mask={label})")
            _run(["databricks", "apps", "create-update", APP_NAME,
                  "--json", f"@{path}", "--profile", profile])
        finally:
            os.unlink(path)


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
    bare = assert_target_host(profile)
    print(f"Target host asserted: {bare}")
    print(f"Deploying app: {APP_NAME}")
    print(f"MAS endpoint (by name, NEVER --recreate): {MAS_ENDPOINT_NAME}")

    app = app_get(profile)
    source_path = (app or {}).get("default_source_code_path") \
        or f"/Workspace/Users/{_current_user(profile)}/apps/{APP_NAME}"

    if dry_run:
        print("\n[dry-run] planned actions:")
        print(f"  1. create app '{APP_NAME}'"
              + (" (SKIP — already exists)" if app else ""))
        print(f"  2. stage clean tree (ship {INCLUDE_FILES} + {INCLUDE_DIRS} + "
              f"{INCLUDE_FRONTEND_DIST}; exclude {sorted(EXCLUDE_ALWAYS)})")
        print(f"  3. workspace import-dir → {source_path}")
        print(f"  4. apps deploy {APP_NAME}")
        create_update(profile, app, dry_run=True)
        print(f"  6. apps deploy {APP_NAME} (redeploy so valueFrom + scopes take effect)")
        print(f"  7. verify RUNNING + resource bound + scope '{SERVING_SCOPE}'")
        print("\n[dry-run] no workspace mutations performed. exit 0.")
        return

    # 2. create app if absent (idempotent).
    if not app:
        print(f"  apps create {APP_NAME}")
        _run(["databricks", "apps", "create", APP_NAME,
              "--description", "FIS R&D Knowledge Agent front door (OBO → MAS)",
              "--profile", profile])
        app = app_get(profile)
        source_path = (app or {}).get("default_source_code_path") or source_path

    # 3-4. stage + import + first deploy.
    staging = stage_tree()
    try:
        import_tree(profile, staging, source_path)
        deploy(profile, source_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 5. bind resource + scopes (read+merge current resources first).
    app = app_get(profile)
    create_update(profile, app, dry_run=False)

    # 6. redeploy so the valueFrom binding + scopes take effect.
    staging = stage_tree()
    try:
        import_tree(profile, staging, source_path)
        deploy(profile, source_path)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 7. verify.
    verify(profile)


def _current_user(profile):
    r = _run(["databricks", "current-user", "me", "--profile", profile, "-o", "json"],
             check=False)
    try:
        return json.loads(r.stdout).get("userName", "unknown")
    except (ValueError, TypeError):
        return "unknown"


def main():
    ap = argparse.ArgumentParser(description="Deploy the FIS R&D front-door Databricks App.")
    ap.add_argument("--profile", required=True, help="Databricks CLI profile (host-asserted).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print planned actions and exit 0 without mutating the workspace.")
    ap.add_argument("--print-url", action="store_true", dest="print_url",
                    help="Print ONLY the running app URL to stdout and exit.")
    args = ap.parse_args()

    if args.print_url:
        # Host-assert even for the read so we never point at the wrong workspace.
        assert_target_host(args.profile)
        print_url(args.profile)
        return

    run_deploy(args.profile, args.dry_run)


if __name__ == "__main__":
    main()
