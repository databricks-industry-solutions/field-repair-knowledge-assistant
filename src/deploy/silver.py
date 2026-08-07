#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 4.1 Plan 01, Task 1: silver layer.

Builds the ENR-01 incremental gate the current `rnd_tickets` table LACKS:
a `content_hash` column keyed on `number`, plus a light location parse and a
per-note-entry table. Ports the deterministic shape of the reference
`/tmp/servicenow_demo/notebooks/20_silver.py` (content_hash + split_notes) into
this repo's CLI-driven, host-gated harness (mirrors preflight.py / build_ka.py).

Design:
  * Step 0 host-gate: reuse `assert_target_host` from preflight.py — refuse to
    run against any workspace but fevm-serverless-stable-l26d62 (T-4.1-01).
  * No Spark session in this CLI harness → the whole silver build is expressed as
    a single `CREATE OR REPLACE TABLE ... AS SELECT` issued through `run_sql`;
    note_entries is a second statement. `content_hash` is computed IN SQL with
    `substr(sha256(...),1,16)` (avoids the reference's PySpark UDF).
  * `rnd_tickets` is already one row per `number` (deduped) — so NO Window dedup
    step (unlike the reference bronze→silver).
  * CDF enabled on silver (KA/enrichment requirement).
  * Idempotent: CREATE OR REPLACE. `--verify` runs the acceptance assertions.

Usage:
    python3 enrich/silver.py --profile serverless-stable
    python3 enrich/silver.py --profile serverless-stable --verify
"""

import argparse
import sys
from pathlib import Path

# --- Reuse the Phase 1 host-safety gate + SQL runner ------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preflight"))
from preflight import assert_target_host, run_sql  # noqa: E402

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
SRC = f"{FQ}.rnd_tickets"
SILVER = f"{FQ}.rd_tasks_silver"
NOTE_ENTRIES = f"{FQ}.rd_task_note_entries"
DEFAULT_PROFILE = "serverless-stable"

# Location grammar (verified live 2026-07-27): "STATE Site Highway Direction",
# e.g. "NM Texico US-60/US-70/US-84 WB", "TX Loving County 302WB".
# All best-effort regex — location_site is the residue after state/highway/dir
# are removed (per plan: light parser, Claude's discretion).
STATE_RE = r"^([A-Z]{2})\\b"
DIR_RE = r"\\b([NSEW]B)\\b"
# highway tokens: I-40, US-60, RT-58, DE-299, Hwy-2, 302WB, US-281 ...
HWY_RE = r"((?:I|US|RT|SR|DE|Hwy|HW|CR|FM)-?[0-9][0-9A-Za-z/.-]*|[0-9]{2,4}[NSEW]B)"


def build_silver_sql():
    """Single CREATE OR REPLACE TABLE AS SELECT for the silver layer."""
    # NOTE: doubled backslashes in the Python string collapse to the single
    # backslash SQL/Java-regex needs (\b, \\ etc.) once serialized to JSON.
    return f"""
CREATE OR REPLACE TABLE {SILVER}
TBLPROPERTIES (delta.enableChangeDataFeed = true,
  comment = 'Silver: rnd_tickets + content_hash (ENR-01 incremental gate) + light location parse. Key=number; self-contained for enrichment. CDF on.')
AS
SELECT
  number,
  title,
  parent,
  assignment_group,
  assigned_to,
  priority              AS priority_level,
  priority_label,
  status,
  workflow_status,
  location,
  description,
  notes,
  close_notes,
  case_age_days         AS duration_days,
  activity_count        AS num_activities,
  comment_count,
  max_inactivity_gap_days,
  (status LIKE 'Closed%')                                         AS is_closed,
  substr(sha2(concat(coalesce(description,''),
                     coalesce(notes,''),
                     coalesce(close_notes,'')), 256), 1, 16)      AS content_hash,
  regexp_extract(location, '{STATE_RE}', 1)                       AS location_state,
  nullif(regexp_extract(location, '{HWY_RE}', 1), '')             AS location_highway,
  nullif(regexp_extract(location, '{DIR_RE}', 1), '')             AS location_direction,
  trim(
    regexp_replace(
      regexp_replace(
        regexp_replace(
          regexp_replace(location, '^[A-Z]{{2}}\\\\s*', ''),
          '{HWY_RE}', ''),
        '{DIR_RE}', ''),
      '\\\\s+', ' ')
  )                                                               AS location_site,
  concat_ws(':',
    regexp_extract(location, '{STATE_RE}', 1),
    trim(regexp_replace(
      regexp_replace(
        regexp_replace(
          regexp_replace(location, '^[A-Z]{{2}}\\\\s*', ''),
          '{HWY_RE}', ''),
        '{DIR_RE}', ''),
      '\\\\s+', ' '))
  )                                                               AS site_key
FROM {SRC}
""".strip()


def build_note_entries_sql():
    """One row per dated note-log entry (ports split_notes; key=number+entry_seq).

    The `notes` field is an append-only log, one dated entry per line
    (verified live): 'YYYY-MM-DD [#AUTHOR#|Name|- Name -] text'. Split on
    newlines, parse the leading date + author, keep non-trivial lines.
    """
    return f"""
CREATE OR REPLACE TABLE {NOTE_ENTRIES}
TBLPROPERTIES (delta.enableChangeDataFeed = true,
  comment = 'One row per dated note-log entry per task (key=number+entry_seq). Ported from 20_silver.py split_notes.')
AS
WITH exploded AS (
  SELECT number,
         posexplode(split(notes, '\\\\n')) AS (entry_seq, line)
  FROM {SRC}
  WHERE notes IS NOT NULL AND length(trim(notes)) > 0
)
SELECT
  number,
  entry_seq,
  nullif(regexp_extract(line, '^\\\\s*([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})', 1), '') AS note_date,
  nullif(regexp_extract(line,
     '^\\\\s*[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}\\\\s*[-#]?\\\\s*([A-Za-z]+)', 1), '')  AS note_author,
  trim(line)                                                                        AS note_text
FROM exploded
WHERE length(trim(line)) > 3
""".strip()


def run_or_die(label, stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} failed (state={state}).", file=sys.stderr)
        print(stmt, file=sys.stderr)
        sys.exit(4)
    print(f"[silver] {label}: SUCCEEDED")
    return data


def scalar(stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return data[0][0], state


def verify(profile):
    """Run the plan's acceptance assertions; exit non-zero on any failure."""
    print("[silver] --verify: running acceptance assertions...")
    checks = []

    n, st = scalar(f"SELECT count(*) FROM {SILVER}", profile)
    checks.append(("row count == 223", n is not None and int(n) == 223, f"{n} (state={st})"))

    n, st = scalar(
        f"SELECT count(*) FROM {SILVER} WHERE content_hash IS NULL OR length(content_hash)=0",
        profile)
    checks.append(("no null/empty content_hash == 0", n is not None and int(n) == 0, f"{n}"))

    n, st = scalar(f"SELECT count(DISTINCT content_hash) FROM {SILVER}", profile)
    checks.append(("distinct content_hash > 200", n is not None and int(n) > 200, f"{n}"))

    n, st = scalar(f"SELECT count(*) FROM {NOTE_ENTRIES}", profile)
    checks.append(("note_entries count > 0", n is not None and int(n) > 0, f"{n}"))

    # CDF property present on silver.
    state, rows = run_sql(f"SHOW TBLPROPERTIES {SILVER}", profile, WAREHOUSE_ID)
    cdf_on = False
    if state == "SUCCEEDED" and rows:
        for r in rows:
            if r and r[0] == "delta.enableChangeDataFeed" and str(r[1]).lower() == "true":
                cdf_on = True
    checks.append(("delta.enableChangeDataFeed == true", cdf_on, str(cdf_on)))

    print("\n[silver] Acceptance results:")
    all_ok = True
    for label, ok, ev in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  (observed: {ev})")
        all_ok = all_ok and ok
    if not all_ok:
        print("[silver] VERIFY FAILED", file=sys.stderr)
        sys.exit(5)
    print("[silver] VERIFY PASSED")



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
    ap = argparse.ArgumentParser(description="Build the FIS R&D silver layer (content_hash gate).")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true",
                    help="run acceptance assertions (also builds first unless --verify-only)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the build; only run assertions against existing tables")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    host = assert_target_host(args.profile)  # Step 0 — T-4.1-01 host gate
    print(f"[silver] Host gate OK: {host}")

    if not args.verify_only:
        run_or_die("build rd_tasks_silver", build_silver_sql(), args.profile)
        run_or_die("build rd_task_note_entries", build_note_entries_sql(), args.profile)

    if args.verify or args.verify_only:
        verify(args.profile)


if __name__ == "__main__":
    main()
