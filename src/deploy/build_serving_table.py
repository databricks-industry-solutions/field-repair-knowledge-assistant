#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — one serving table for BOTH Genie and the KA.

Today the two engines read two different surfaces:
  * KA    -> rnd_tickets.ka_content            (text + metadata struct, for citations)
  * Genie -> rd_tasks_gold_analytics (a VIEW)  (structured columns, for SQL)

`ka_content` is a COPY of the enrichment segments written onto rnd_tickets, so
re-running enrichment leaves the KA serving stale text until build_ka_content.py
runs again. This script builds a single physical table carrying everything both
engines need, so one refresh serves both.

WHY A TABLE AND NOT A VIEW (verified live against the KA API):
    Attaching a view fails —
      "must either be a streaming table or have Change Data Feed enabled.
       To enable CDF: ALTER TABLE ... SET TBLPROPERTIES (...)"
    CDF is not settable on a view, so the KA source MUST be a physical Delta
    table. That is the whole reason this is a CTAS + MERGE and not a `CREATE VIEW`.

Two other KA constraints this table satisfies, both confirmed by live probes:
  * `metadata` STRUCT is REQUIRED. Attaching a table without it fails with
    "missing required column '_metadata'" BEFORE any column-choice validation.
  * `file_col` names EXACTLY ONE content column ("Array must have size 1, but has
    size 2"). So `ka_content` must be pre-composed — the KA cannot index several
    columns and cannot infer one.

Composition of `ka_content` is IMPORTED from build_ka_content.py rather than
re-implemented, so the serving table is a pure refactor: retrieval behaviour (and
therefore the Phase 07 eval numbers) stays identical.

Grain: one row per task. silver, enrichment and rnd_tickets are all 223 rows and
join 1:1 on `number` (asserted by --verify).

Usage:
    python3 enrich/build_serving_table.py --profile serverless-stable
    python3 enrich/build_serving_table.py --profile serverless-stable --verify
    python3 enrich/build_serving_table.py --profile serverless-stable --dry-run
"""

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preflight"))
from preflight import assert_target_host, run_sql  # noqa: E402
sys.path.insert(0, str(REPO / "enrich"))
from build_glossary import run_sql_poll  # noqa: E402
# Reuse the PROVEN content recipe — do not fork it (a second copy would drift and
# silently change retrieval quality).
from build_ka_content import (  # noqa: E402
    KA_CONTENT_COL,
    SEGMENT_COLS,
    expand_expr,
    load_acronym_map,
)

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"

T_SILVER = f"{FQ}.rd_tasks_silver"
T_TICKETS = f"{FQ}.rnd_tickets"
T_GOLD_ENRICH = f"{FQ}.rd_tasks_gold_enrichment"
T_NOTE_ENTRIES = f"{FQ}.rd_task_note_entries"
T_SERVING = f"{FQ}.rd_tasks_serving"
DEFAULT_PROFILE = "serverless-stable"

EXPECTED_ROWS = 223


def serving_select(acr_map):
    """The SELECT that composes the serving row: silver + enrichment + content.

    Column choices:
      * structured columns come from SILVER (the Genie-facing grain: priority,
        status, location parts, durations).
      * derived columns come from ENRICHMENT (systems/vendors/category/segments).
      * `case_text` and `metadata` come from RND_TICKETS — metadata is what makes
        a KA hit citeable, and case_text is kept so the raw ticket stays queryable
        (and so file_col can be A/B'd later without rebuilding).
      * `ka_content` is composed HERE with the imported recipe.
    """
    parts = [expand_expr(f"coalesce(e.{c}, '')", acr_map) for c in SEGMENT_COLS]
    ka_content = "concat_ws('\\n', " + ", ".join(parts) + ")"
    return f"""
SELECT
  -- identity
  s.number,
  s.content_hash,
  -- structured (Genie): straight from silver
  s.title, s.parent, s.assignment_group, s.assigned_to,
  s.priority_level, s.priority_label, s.status, s.workflow_status, s.is_closed,
  s.location, s.location_state, s.location_highway, s.location_direction,
  s.location_site, s.site_key,
  s.duration_days, s.num_activities, s.comment_count, s.max_inactivity_gap_days,
  coalesce(ne.num_note_entries, 0) AS num_note_entries,
  -- raw narrative (kept queryable; also the A/B alternative for file_col)
  s.description, s.notes, s.close_notes, t.case_text,
  -- derived (enrichment)
  e.systems_involved, e.hardware_mentioned, e.vendors, e.problem_category,
  e.summary, e.customer_impact, e.troubleshooting, e.recommendation,
  e.root_cause, e.resolution, e.resolution_type,
  e.conf_systems, e.conf_root_cause, e.conf_resolution_type,
  e.min_confidence, e.needs_review,
  e.prompt_version, e.model, e.enriched_at,
  -- KA: the ONE indexed content column, pre-composed (file_col takes exactly one)
  {ka_content} AS {KA_CONTENT_COL},
  -- KA: REQUIRED struct — without it the attach fails outright
  t.metadata
FROM {T_SILVER} s
LEFT JOIN {T_TICKETS} t ON s.number = t.number
LEFT JOIN {T_GOLD_ENRICH} e ON s.number = e.number
LEFT JOIN (SELECT number, count(*) AS num_note_entries
           FROM {T_NOTE_ENTRIES} GROUP BY number) ne ON s.number = ne.number
""".strip()


def sql_create(acr_map):
    """CTAS + CDF. CDF is MANDATORY for the KA source (verified: attach fails without)."""
    return f"""
CREATE OR REPLACE TABLE {T_SERVING}
COMMENT 'One serving row per FIS R&D task for BOTH engines. KA indexes ka_content (segmented + glossary-acronym-expanded) and cites via the metadata struct; Genie reads the structured columns. Built by enrich/build_serving_table.py from silver + rd_tasks_gold_enrichment + rnd_tickets.'
TBLPROPERTIES (delta.enableChangeDataFeed = true)
AS {serving_select(acr_map)}
""".strip()


def sql_refresh(acr_map):
    """Idempotent refresh: MERGE on number (no rebuild, so CDF history survives)."""
    return f"""
MERGE INTO {T_SERVING} tgt
USING ({serving_select(acr_map)}) src
ON tgt.number = src.number
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""".strip()


# --- Genie serving view over the ONE table ----------------------------------
# Genie reads column COMMENTs to write correct SQL, so the curated view is kept
# (COMMENTs are the text-to-SQL hints). It now sits on the serving table, not on
# a silver-join chain — so both engines resolve to the SAME physical rows.

def sql_analytics_view():
    return f"""
CREATE OR REPLACE VIEW {FQ}.rd_tasks_serving_analytics (
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

def run_or_die(label, stmt, profile, poll=False):
    runner = run_sql_poll if poll else run_sql
    state, data = runner(stmt, profile, WAREHOUSE_ID)
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


def table_exists(profile):
    st, _ = run_sql(f"DESCRIBE {T_SERVING}", profile, WAREHOUSE_ID)
    return st == "SUCCEEDED"


def build(profile, dry_run=False):
    print("[serving] Step 1: acronym map FROM approved glossary (GLO-02)...")
    acr_map = load_acronym_map(profile)
    print(f"[serving]   {len(acr_map)} acronyms: {sorted(acr_map)}")
    if not acr_map:
        print("FATAL: no approved acronyms — refusing to build unexpanded content.",
              file=sys.stderr)
        sys.exit(4)

    exists = table_exists(profile)
    stmt = sql_refresh(acr_map) if exists else sql_create(acr_map)
    label = ("MERGE refresh rd_tasks_serving" if exists
             else "CREATE rd_tasks_serving (CTAS + CDF)")

    if dry_run:
        print(f"\n[dry-run] would run: {label}\n")
        print(stmt[:3000])
        print(f"\n[dry-run] would run: rd_tasks_serving_analytics view")
        print("\n[dry-run] no mutations. exit 0.")
        return

    print(f"[serving] Step 2: {label}...")
    run_or_die(label, stmt, profile, poll=True)

    print("[serving] Step 3: rd_tasks_serving_analytics (Genie surface)...")
    run_or_die("rd_tasks_serving_analytics view", sql_analytics_view(), profile)


# --- --verify ---------------------------------------------------------------

def verify(profile):
    print("[verify] rd_tasks_serving acceptance checks\n")
    checks = []

    n, _ = scalar(f"SELECT count(*) FROM {T_SERVING}", profile)
    checks.append((f"row count == {EXPECTED_ROWS}",
                   n is not None and int(n) == EXPECTED_ROWS, str(n)))

    # KA hard requirements (both verified live as attach-blocking).
    st, cols = run_sql(f"DESCRIBE {T_SERVING}", profile, WAREHOUSE_ID)
    names = [r[0] for r in (cols or []) if r and r[0] and not r[0].startswith("#")]
    checks.append(("metadata struct present (KA attach requires it)",
                   "metadata" in names, "present" if "metadata" in names else "MISSING"))
    checks.append((f"{KA_CONTENT_COL} present (the one indexed column)",
                   KA_CONTENT_COL in names, "present" if KA_CONTENT_COL in names else "MISSING"))

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

    # Acronym expansion actually applied (same assertion build_ka_content makes).
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

    # PARITY: ka_content must match what the KA serves today, or this is not a
    # pure refactor and the Phase 07 eval numbers stop describing it.
    n, _ = scalar(
        f"SELECT count(*) FROM {T_SERVING} s JOIN {T_TICKETS} t ON s.number = t.number "
        f"WHERE s.{KA_CONTENT_COL} <> t.{KA_CONTENT_COL}", profile)
    checks.append(("ka_content IDENTICAL to rnd_tickets.ka_content (pure refactor)",
                   n is not None and int(n) == 0, f"{n} differing"))

    # Genie surface reads.
    n, _ = scalar(f"SELECT count(*) FROM {FQ}.rd_tasks_serving_analytics", profile)
    checks.append((f"analytics view readable == {EXPECTED_ROWS}",
                   n is not None and int(n) == EXPECTED_ROWS, str(n)))

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
    import re as _re
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
        description="Build the single rd_tasks_serving table for Genie + KA.")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true", help="run acceptance checks only")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the SQL without mutating anything")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    # Host gate BEFORE any write (CLAUDE.md platform constraint).
    assert_target_host(args.profile)

    if args.verify:
        verify(args.profile)
        return
    build(args.profile, dry_run=args.dry_run)
    if not args.dry_run:
        print()
        verify(args.profile)


if __name__ == "__main__":
    main()
