#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — LOCAL Genie analytics views over rd_tasks_serving + KA/Genie checks.

The consolidated `rd_tasks_serving` table (the single surface BOTH engines read —
KA indexes `ka_content`, Genie reads the structured columns) is built IN-JOB by the
`serving` notebook (src/notebooks/serving.py), which also creates the analytics views and
runs verify(). This script is a LOCAL convenience CLI (run from a laptop with a warehouse):
it is NOT a task in the fis_data_pipeline job. It owns the two warehouse-layer operations:

  1. The curated Genie serving views over rd_tasks_serving:
       * rd_tasks_serving_analytics  — the canonical, COMMENTed Genie surface.
       * rd_tasks_gold_analytics     — a compatibility view of the same shape,
         kept because the supervisor grants and the Genie/supervisor tests still
         reference that legacy name.
     The column COMMENTs are Genie's text-to-SQL hints, and `WITH SCHEMA
     COMPENSATION` keeps them exactly as Genie expects.
  2. `--verify`: the KA/Genie acceptance checks against the notebook-built table
     (metadata struct present, CDF enabled, one pre-composed ka_content column,
     citation file_path present, 1:1 grain, enrichment populated).

Usage:
    python3 src/deploy/build_serving_table.py --profile serverless-stable
    python3 src/deploy/build_serving_table.py --profile serverless-stable --verify
    python3 src/deploy/build_serving_table.py --profile serverless-stable --analytics-only
"""

import argparse
import os
import sys
from pathlib import Path

# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally. preflight.py
# and env.py live in THIS dir (the old REPO/"preflight" path no longer exists).
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO = _HERE.parent
sys.path.insert(0, str(_HERE))
from preflight import assert_target_host, run_sql  # noqa: E402

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"

T_TICKETS = f"{FQ}.rnd_tickets"
T_SERVING = f"{FQ}.rd_tasks_serving"
KA_CONTENT_COL = "ka_content"  # the one indexed KA content column on rd_tasks_serving
DEFAULT_PROFILE = "serverless-stable"

# Demo-specific corpus size. Left UNSET (0) by default so the template works for any
# corpus — the check then only asserts the serving table is non-empty. Pin it via
# FIS_EXPECTED_ROWS=<n> to hard-assert an exact count for a specific demo dataset.
EXPECTED_ROWS = int(os.environ.get("FIS_EXPECTED_ROWS", "0"))


# --- Genie serving view over the ONE table ----------------------------------
# Genie reads column COMMENTs to write correct SQL, so the curated view is kept
# (COMMENTs are the text-to-SQL hints). It now sits on the serving table, not on
# a silver-join chain — so both engines resolve to the SAME physical rows.

def sql_analytics_view(view="rd_tasks_serving_analytics"):
    return f"""
CREATE OR REPLACE VIEW {FQ}.{view} (
  task_number COMMENT 'R&D task id, e.g. R&DTASK0001070. Unique key.',
  title COMMENT 'Short description of the task/issue.',
  parent_case COMMENT 'Parent support case id (SDC...).',
  assigned_to COMMENT 'R&D team member who owns the task (domain-expert signal).',
  priority_level COMMENT '1=Critical..4=Low. Lower number = higher priority.',
  priority_label COMMENT 'Text priority: Critical/High/Moderate/Low.',
  status COMMENT 'Open, Pending, Closed Complete, Closed Skipped, Closed Incomplete.',
  workflow_status COMMENT 'Draft, Assigned, Work in progress, Completed.',
  is_closed COMMENT 'TRUE if status starts with Closed.',
  location_state COMMENT '2-letter US state / Canadian province of the site.',
  location_highway COMMENT 'Highway/route of the site, e.g. I-40, US-60.',
  location_site COMMENT 'Site name within the state, e.g. Texico, Orange Grove.',
  site_key COMMENT 'Canonical state:site key for grouping tasks by site.',
  duration_days COMMENT 'Days between first and last activity. Higher = longer to resolve.',
  num_note_entries COMMENT 'Count of dated note entries; proxy for back-and-forth/difficulty.',
  num_activities COMMENT 'Count of audit-trail activity events.',
  systems_involved COMMENT 'Array of screening systems: ALPR, ATIS, WIM, HTS, OVC, etc. Use array_contains().',
  hardware_mentioned COMMENT 'Array of hardware/components mentioned.',
  vendors COMMENT 'Array of vendors: Neology, Kistler, PIPS, etc.',
  problem_category COMMENT 'hardware_failure, software_crash, network_connectivity, calibration, image_quality, power, configuration, other.',
  summary COMMENT 'LLM-segmented: what the issue is (from the description).',
  customer_impact COMMENT 'LLM-segmented: effect on the customer/site. Empty if the ticket does not state one.',
  troubleshooting COMMENT 'LLM-segmented: steps already taken to verify or resolve.',
  recommendation COMMENT 'LLM-segmented: proposed fix / parts required.',
  root_cause COMMENT 'One-sentence LLM-extracted root cause, or undetermined.',
  resolution COMMENT 'What resolved the issue, or unresolved.',
  resolution_type COMMENT 'hardware_replace, software_patch, recalibration, config_change, rma, firmware_update, monitoring, no_fix_found, unresolved, not_applicable.',
  needs_review COMMENT 'TRUE if enrichment confidence was low (SME should verify).')
COMMENT 'Curated R&D task analytics for Fleetworthy roadside truck-screening (WIM/ALPR/AUR/ATIS). One row per task over rd_tasks_serving — the SAME physical rows the Knowledge Assistant retrieves from. Use for counts, durations, expert-finding, priority and site-pattern analysis.'
WITH SCHEMA COMPENSATION
AS SELECT number, title, parent, assigned_to, priority_level, priority_label, status,
  workflow_status, is_closed, location_state, location_highway, location_site, site_key,
  duration_days, num_note_entries, num_activities,
  systems_involved, hardware_mentioned, vendors, problem_category,
  summary, customer_impact, troubleshooting, recommendation,
  root_cause, resolution, resolution_type, needs_review
FROM {T_SERVING}
""".strip()


# --- runners ----------------------------------------------------------------

def run_or_die(label, stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} failed (state={state}).", file=sys.stderr)
        print(stmt[:2000], file=sys.stderr)
        sys.exit(4)
    print(f"[serving] {label}: SUCCEEDED")
    return data


def scalar(stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return data[0][0], state


def _build_analytics_views(profile):
    """Create the curated Genie view + the rd_tasks_gold_analytics compat alias.

    Both are the same curated, COMMENTed surface over rd_tasks_serving.
    `rd_tasks_serving_analytics` is the canonical name; `rd_tasks_gold_analytics`
    is kept as a compatibility view because the supervisor grants and the
    Genie/supervisor tests still reference that legacy name (the enrich pipeline no
    longer creates the old silver-join gold view).
    """
    run_or_die("rd_tasks_serving_analytics view",
               sql_analytics_view("rd_tasks_serving_analytics"), profile)
    run_or_die("rd_tasks_gold_analytics view (compat)",
               sql_analytics_view("rd_tasks_gold_analytics"), profile)


# --- --verify ---------------------------------------------------------------

def verify(profile):
    print("[verify] rd_tasks_serving acceptance checks\n")
    checks = []

    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING}", profile)
    if EXPECTED_ROWS:
        checks.append((f"row count == {EXPECTED_ROWS}",
                       n is not None and int(n) == EXPECTED_ROWS, str(n)))
    else:
        checks.append(("row count > 0 (corpus non-empty)",
                       n is not None and int(n) > 0, str(n)))

    # KA hard requirements (both verified live as attach-blocking).
    st, cols = run_sql(f"DESCRIBE {T_SERVING}", profile, WAREHOUSE_ID)
    names = [r[0] for r in (cols or []) if r and r[0] and not r[0].startswith("#")]
    checks.append(("metadata struct present (KA attach requires it)",
                   "metadata" in names, "present" if "metadata" in names else "MISSING"))
    checks.append((f"{KA_CONTENT_COL} present (the one indexed column)",
                   KA_CONTENT_COL in names, "present" if KA_CONTENT_COL in names else "MISSING"))

    # rd_tasks_serving MUST be a real TABLE, not a view/materialized view: the KA sync
    # streams from it, and streaming from an MV fails even with CDF nominally set. This
    # check closes the gap that let the MV regression pass CDF-only verification.
    _, tt = run_sql(
        f"SELECT table_type FROM {CATALOG}.information_schema.tables "
        f"WHERE table_schema='{SCHEMA}' AND table_name='rd_tasks_serving'",
        profile, WAREHOUSE_ID)
    ttype = tt[0][0] if (tt and tt[0]) else ""
    checks.append(("rd_tasks_serving is a TABLE, not a view/MV (KA streaming attach)",
                   str(ttype).upper() in ("MANAGED", "EXTERNAL", "BASE TABLE", "MANAGED_TABLE"),
                   str(ttype)))

    st, props = run_sql(f"SHOW TBLPROPERTIES {T_SERVING}", profile, WAREHOUSE_ID)
    cdf = any(r and "changeDataFeed" in r[0] and str(r[1]).lower() in ("true", "supported")
              for r in (props or []))
    checks.append(("CDF enabled (KA attach requires it)", cdf, "on" if cdf else "OFF"))

    # Content integrity.
    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING} "
                  f"WHERE {KA_CONTENT_COL} IS NULL OR length({KA_CONTENT_COL}) = 0", profile)
    checks.append(("no null/empty ka_content == 0", n is not None and int(n) == 0, str(n)))

    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING} WHERE metadata.file_path IS NULL", profile)
    checks.append(("every row has a citation file_path == 0 null",
                   n is not None and int(n) == 0, str(n)))

    # Acronym expansion actually applied (the pipeline composed ka_content).
    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING} "
                  f"WHERE {KA_CONTENT_COL} LIKE '%AUR (%'", profile)
    checks.append(("acronym expansion present (ka_content LIKE 'AUR (%')",
                   n is not None and int(n) > 0, str(n)))

    # Grain: 1:1, no fan-out from the joins.
    n, _ = scalar(f"SELECT count(*) - count(DISTINCT number) FROM {T_SERVING}", profile)
    checks.append(("number is unique (no join fan-out)",
                   n is not None and int(n) == 0, str(n)))

    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING} WHERE problem_category IS NULL", profile)
    checks.append(("every row carries enrichment == 0 null category",
                   n is not None and int(n) == 0, str(n)))

    # PARITY (legacy, warehouse path only): historically ka_content had to match the
    # copy on rnd_tickets. The serving notebook composes ka_content directly in
    # rd_tasks_serving and no longer populates rnd_tickets.ka_content, so this check
    # is skipped when that legacy column is absent (its parity target is gone).
    st_t, cols_t = run_sql(f"DESCRIBE {T_TICKETS}", profile, WAREHOUSE_ID)
    tickets_has_ka = any(r and r[0] == KA_CONTENT_COL for r in (cols_t or []))
    if tickets_has_ka:
        n, _ = scalar(
            f"SELECT count(*) FROM {T_SERVING} s JOIN {T_TICKETS} t ON s.number = t.number "
            f"WHERE s.{KA_CONTENT_COL} <> t.{KA_CONTENT_COL}", profile)
        checks.append(("ka_content IDENTICAL to rnd_tickets.ka_content (pure refactor)",
                       n is not None and int(n) == 0, f"{n} differing"))
    else:
        print("  [SKIP] ka_content parity vs rnd_tickets.ka_content "
              "(pipeline composes ka_content in rd_tasks_serving; legacy column absent)")

    # Genie surface reads.
    n, _ = scalar(f"SELECT count(*) FROM {FQ}.rd_tasks_serving_analytics", profile)
    if EXPECTED_ROWS:
        checks.append((f"analytics view readable == {EXPECTED_ROWS}",
                       n is not None and int(n) == EXPECTED_ROWS, str(n)))
    else:
        checks.append(("analytics view readable (rows > 0)",
                       n is not None and int(n) > 0, str(n)))

    ok = True
    for label, passed, got in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} (got {got})")
        ok = ok and passed
    print()
    if not ok:
        print("VERIFY FAILED.", file=sys.stderr)
        sys.exit(6)
    print("VERIFY PASSED — rd_tasks_serving satisfies BOTH the KA attach "
          "requirements and the Genie read surface.")



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
    ap = argparse.ArgumentParser(
        description="LOCAL: Genie analytics views over the notebook-built rd_tasks_serving, "
                    "plus the KA/Genie acceptance checks.")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true", help="run acceptance checks only")
    ap.add_argument("--analytics-only", action="store_true",
                    help="only (re)create the Genie analytics views over "
                         "rd_tasks_serving (skip the acceptance checks).")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    # Host gate BEFORE any write (CLAUDE.md platform constraint).
    assert_target_host(args.profile)

    if args.verify:
        verify(args.profile)
        return
    # Default (and --analytics-only): (re)create the Genie analytics views over
    # rd_tasks_serving. The serving TABLE itself is built in-job by the `serving` notebook
    # (src/notebooks/serving.py), not here — this is a local convenience path.
    print("[serving] (re)create the Genie analytics views over rd_tasks_serving...")
    _build_analytics_views(args.profile)
    if not args.analytics_only:
        print()
        verify(args.profile)


if __name__ == "__main__":
    main()
