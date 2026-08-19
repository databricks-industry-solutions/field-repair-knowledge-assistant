#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant — Phase 6 Front-Door OBO Preflight probe.

Re-runnable probe of the ONE hard blocker for Phase 6 (front door): whether
Databricks Apps **user authorization** (Public Preview) — the on-behalf-of (OBO)
mechanism FD-02 depends on — is enabled on the target workspace, plus the exact
serving OAuth scope string the workspace offers (RESEARCH assumption A1 flagged
`serving.serving-endpoints` at MEDIUM confidence).

Why this exists (RESEARCH Environment Availability — "must preflight — hard blocker"):
  If user authorization is disabled, OBO is impossible without a workspace-admin
  action. Fail fast in Wave 1; the confirmed scope is applied at deploy (06-05).

Design (mirrors preflight/preflight.py conventions):
  - Step 0 host-assertion gate: refuses to run any check unless the resolved
    Databricks host is the reference workspace. Prevents silently probing
    the wrong workspace.
  - Read-only: enumerates app + scope + endpoint metadata only. It NEVER creates,
    updates, or deploys anything, NEVER calls the MAS, and NEVER mints, prints,
    or logs a token (threat T-06-01 / T-06-02).
  - Honest reporting: probe success (a clean read) is NOT the same as the feature
    being enabled. Enablement is CONFIRMED BY A HUMAN in Task 2 against the live
    scope picker; this probe only gathers evidence and prints a finding.

Usage:
    python frontdoor/preflight_obo.py --profile serverless-stable
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

TARGET_HOST_FRAGMENT = os.environ.get("RKB_TARGET_HOST", "")
TARGET_WORKSPACE_ID = os.environ.get("RKB_TARGET_WORKSPACE_ID", "")
DEFAULT_PROFILE = "serverless-stable"

# The MAS front door will call this endpoint on behalf of the end user (FD-02).
MAS_ENDPOINT_NAME = os.environ.get("MAS_ENDPOINT_NAME", "")
DEMO_PRINCIPAL = os.environ.get("RKB_PRINCIPAL", "")

# RESEARCH A1 (MEDIUM confidence): the serving OBO scope string sourced from the
# AppKit model-serving skill reference. This probe records whether the live
# workspace surface (apps manifest / serving plugin) corroborates it; the exact
# literal string is CONFIRMED BY THE HUMAN in Task 2 against the scope picker.
CANDIDATE_SERVING_SCOPE = "serving.serving-endpoints"

# Never emit anything matching these — belt-and-suspenders against token leakage.
_TOKEN_PATTERNS = [
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"x-forwarded-access-token", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9._-]{20,}"),  # JWT-shaped
]


def _scrub(text):
    """Defensive: redact anything token-shaped before it can reach stdout/stderr."""
    if not text:
        return text
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


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


# --- Step 0: host-assertion gate (mirrors preflight/preflight.py) ------------

def assert_target_host(profile):
    """HARD GATE. Returns resolved host or exits non-zero if not the target."""
    code, out, err = run_cli(["auth", "env"], profile)
    if code != 0:
        print(f"FATAL: profile '{profile}' auth invalid ({err or 'auth env failed'}).",
              file=sys.stderr)
        print("Fix: databricks auth login --host "
              f"https://{TARGET_HOST_FRAGMENT}.cloud.databricks.com "
              f"--profile {profile}", file=sys.stderr)
        sys.exit(2)
    try:
        env = json.loads(out).get("env", {})
    except json.JSONDecodeError:
        env = {}
    host = env.get("DATABRICKS_HOST", "")
    if TARGET_HOST_FRAGMENT not in host:
        print(f"FATAL: resolved host '{host}' is not the target "
              f"({TARGET_HOST_FRAGMENT}). Refusing to probe the wrong workspace.",
              file=sys.stderr)
        sys.exit(3)
    return host


def resolve_principal(profile):
    code, out, _ = run_cli(["current-user", "me"], profile)
    if code != 0:
        return "unknown"
    try:
        return json.loads(out).get("userName", "unknown")
    except json.JSONDecodeError:
        return "unknown"


# --- Probe 1: user-authorization enablement surface -------------------------

def probe_user_authorization(profile):
    """Enumerate apps and inspect the user-authorization scope surface.

    The Apps API exposes two fields that only exist because the workspace runs a
    user-authorization-aware Apps control plane:
      - user_api_scopes: scopes an app has REQUESTED (None until an app opts in)
      - effective_user_api_scopes: scopes actually GRANTED to the app's users

    A populated effective_user_api_scopes on any app is strong evidence the
    framework surface is live. Definitive enablement (the togglable "User
    authorization" section + scope picker) is confirmed by the human in Task 2.
    """
    code, out, err = run_cli(["apps", "list", "-o", "json"], profile)
    if code != 0:
        return {
            "surface_reachable": False,
            "apps_probed": 0,
            "any_effective_scopes": False,
            "observed_effective_scopes": [],
            "apps_requesting_scopes": [],
            "note": f"apps list failed ({_scrub(err) or code})",
        }
    try:
        apps = json.loads(out) or []
    except json.JSONDecodeError:
        apps = []

    observed = set()
    requesting = []
    for app in apps:
        name = app.get("name", "?")
        # Per-app get exposes effective_user_api_scopes (list may be absent on list view).
        gcode, gout, _ = run_cli(["apps", "get", name, "-o", "json"], profile)
        if gcode != 0:
            continue
        try:
            detail = json.loads(gout)
        except json.JSONDecodeError:
            continue
        eff = detail.get("effective_user_api_scopes") or []
        req = detail.get("user_api_scopes") or []
        observed.update(eff)
        if req:
            requesting.append({"app": name, "user_api_scopes": req})

    return {
        "surface_reachable": True,
        "apps_probed": len(apps),
        # The effective_user_api_scopes field being populated at all is the signal
        # that the user-authorization framework is present on this control plane.
        "any_effective_scopes": bool(observed),
        "observed_effective_scopes": sorted(observed),
        "apps_requesting_scopes": requesting,
        "note": ("effective_user_api_scopes surface present"
                 if observed else "no effective scopes observed on any app"),
    }


# --- Probe 2: serving scope string offered by the workspace -----------------

def probe_serving_scope(profile):
    """Corroborate the serving OBO scope against the live apps manifest.

    `databricks apps manifest` enumerates the AppKit plugins/resources the
    workspace offers. The `serving` plugin declaring a serving_endpoint resource
    with CAN_QUERY is the surface that (in the UI create/edit flow) drives the
    `serving.serving-endpoints` scope request. We record whether that surface is
    present; the EXACT literal scope string is confirmed by the human in Task 2.
    """
    code, out, err = run_cli(["apps", "manifest", "-o", "json"], profile)
    manifest_ok = code == 0
    serving_plugin = False
    serving_can_query = False
    literal_scope_in_manifest = False
    if manifest_ok:
        # Substring scan is sufficient and avoids brittle schema assumptions.
        blob = out
        serving_plugin = '"serving"' in blob or "Model Serving Plugin" in blob
        serving_can_query = "serving_endpoint" in blob and "CAN_QUERY" in blob
        literal_scope_in_manifest = CANDIDATE_SERVING_SCOPE in blob

    return {
        "manifest_reachable": manifest_ok,
        "serving_plugin_present": serving_plugin,
        "serving_endpoint_can_query_present": serving_can_query,
        "candidate_scope": CANDIDATE_SERVING_SCOPE,
        "candidate_scope_literal_in_manifest": literal_scope_in_manifest,
        "confidence": "MEDIUM — from AppKit skill ref; human confirms literal string in Task 2",
        "note": ("serving plugin + CAN_QUERY surface present"
                 if (serving_plugin and serving_can_query)
                 else _scrub(err) or "serving surface not fully observed"),
    }


# --- Probe 3: demo principal CAN_QUERY on the MAS endpoint (OBO precondition) -

def probe_endpoint_can_query(profile):
    """OBO evaluates the USER's grants (RESEARCH Pitfall 3). Confirm the demo
    principal can query the MAS endpoint directly — a precondition for the OBO
    path to succeed for that user."""
    code, out, _ = run_cli(["serving-endpoints", "get", MAS_ENDPOINT_NAME, "-o", "json"],
                           profile)
    if code != 0:
        return {"endpoint": MAS_ENDPOINT_NAME, "reachable": False,
                "ready": False, "demo_principal_can_query": None,
                "note": "endpoint get failed"}
    try:
        ep = json.loads(out)
    except json.JSONDecodeError:
        return {"endpoint": MAS_ENDPOINT_NAME, "reachable": False,
                "ready": False, "demo_principal_can_query": None,
                "note": "endpoint get unparseable"}
    ep_id = ep.get("id", "")
    ready = (ep.get("state") or {}).get("ready") == "READY"

    can_query = None
    pcode, pout, _ = run_cli(
        ["serving-endpoints", "get-permissions", ep_id, "-o", "json"], profile)
    if pcode == 0:
        try:
            acl = json.loads(pout).get("access_control_list", [])
        except json.JSONDecodeError:
            acl = []
        for entry in acl:
            who = (entry.get("user_name") or entry.get("group_name")
                   or entry.get("service_principal_name") or "")
            perms = {p.get("permission_level") for p in entry.get("all_permissions", [])}
            if who == DEMO_PRINCIPAL and ("CAN_QUERY" in perms or "CAN_MANAGE" in perms):
                can_query = True
        if can_query is None:
            can_query = False
    return {
        "endpoint": MAS_ENDPOINT_NAME,
        "reachable": True,
        "ready": ready,
        "demo_principal": DEMO_PRINCIPAL,
        "demo_principal_can_query": can_query,
        "note": "READY + demo principal CAN_QUERY" if (ready and can_query)
                else "see fields",
    }


def main():
    ap = argparse.ArgumentParser(description="Phase 6 front-door OBO preflight probe")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    args = ap.parse_args()

    # Step 0 — never probe the wrong/unauthenticated workspace.
    host = assert_target_host(args.profile)
    principal = resolve_principal(args.profile)

    user_auth = probe_user_authorization(args.profile)
    serving_scope = probe_serving_scope(args.profile)
    endpoint = probe_endpoint_can_query(args.profile)

    summary = {
        "probe": "phase6-obo-preflight",
        "host": host,
        "workspace_id": TARGET_WORKSPACE_ID,
        "principal": principal,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_authorization": user_auth,
        "serving_scope": serving_scope,
        "endpoint": endpoint,
        # Probe success != feature enabled. Enablement is confirmed by the human
        # in Task 2 against the live "User authorization" scope picker.
        "verdict_note": ("PROBE_CLEAN — surfaces read; ENABLEMENT + exact scope "
                         "string require human confirmation (Task 2)"),
    }

    # Machine-readable summary line (single line, token-scrubbed).
    print("PREFLIGHT_OBO_SUMMARY " + _scrub(json.dumps(summary, separators=(",", ":"))))

    # Human-readable findings.
    print("\n--- Phase 6 OBO preflight findings ---")
    print(f"host: {host}  principal: {principal}")
    print(f"user-authorization surface reachable: {user_auth['surface_reachable']}; "
          f"apps probed: {user_auth['apps_probed']}; "
          f"effective_user_api_scopes present: {user_auth['any_effective_scopes']} "
          f"{user_auth['observed_effective_scopes']}")
    print(f"apps currently requesting user_api_scopes: "
          f"{user_auth['apps_requesting_scopes'] or 'none (all SP-only)'}")
    print(f"serving scope (candidate, RESEARCH A1): {serving_scope['candidate_scope']} "
          f"[MEDIUM — confirm literal string in scope picker, Task 2]")
    print(f"  serving plugin present: {serving_scope['serving_plugin_present']}; "
          f"serving_endpoint+CAN_QUERY surface: "
          f"{serving_scope['serving_endpoint_can_query_present']}")
    print(f"MAS endpoint {endpoint['endpoint']}: reachable={endpoint['reachable']} "
          f"ready={endpoint['ready']} "
          f"demo-principal CAN_QUERY={endpoint.get('demo_principal_can_query')}")
    print("\nENABLEMENT is NOT asserted by this probe — Task 2 (human-verify) confirms "
          "the 'User authorization' toggle + exact scope string in the live workspace.")

    # Exit 0 on a clean probe (all three read-only probes reached their surfaces).
    clean = (user_auth["surface_reachable"] and serving_scope["manifest_reachable"]
             and endpoint["reachable"])
    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
