#!/usr/bin/env python3
"""
Knowledge Agent — silver layer, built in the data-generation step.

Output: `rd_tasks_silver` (+ the per-note-entry table `rd_task_note_entries`),
derived from the bronze `rnd_tickets` corpus — which already contains BOTH the
real sample tickets and the appended synthetic tickets (load_tables
--append-synthetic). So the silver table this produces IS the synth-inclusive
corpus, typed and with the ENR-01 `content_hash` gate + a light location parse.

This lives with data generation (not in the enrich/serving notebooks) on purpose:
silver is the shaped corpus the demo data-generation flow produces, and the enrich
notebook (rd_tasks_gold_enrichment) reads `rd_tasks_silver`. CDF is enabled so the
table is a clean Delta source; the `content_hash` column it computes is the incremental
gate the enrich notebook's LEFT ANTI JOIN uses (a re-run with no new tickets does zero
LLM work; a changed ticket's content_hash changes and is re-enriched next run).

No Spark session — the whole build is SQL issued through `run_sql` (host-gated via
preflight `assert_target_host`), mirroring the other data-prep scripts.

Usage:
    python3 data_generation/build_silver.py --profile serverless-stable
    python3 data_generation/build_silver.py --profile serverless-stable --verify
"""

import argparse
import sys
from pathlib import Path

# The shared helpers (preflight/env) live in src/deploy; put it on the path so
# this data-generation script resolves them the same way the deploy scripts do.
# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally.
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO_ROOT = _HERE.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "deploy"))
from preflight import assert_target_host, run_sql  # noqa: E402
import env as _env  # noqa: E402

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
SRC = f"{FQ}.rnd_tickets"
SILVER = f"{FQ}.rd_tasks_silver"
NOTE_ENTRIES = f"{FQ}.rd_task_note_entries"
DEFAULT_PROFILE = "serverless-stable"

# Location grammar (verified live): "STATE Site Highway Direction",
# e.g. "NM Sandia US-60/US-70/US-84 WB", "TX Rio Seco 302WB". Doubled
# backslashes collapse to the single backslash the SQL/Java regex needs once the
# statement is serialized to JSON for the SQL Statements API.
STATE_RE = r"^([A-Z]{2})\\b"
DIR_RE = r"\\b([NSEW]B)\\b"
HWY_RE = r"((?:I|US|RT|SR|DE|Hwy|HW|CR|FM)-?[0-9][0-9A-Za-z/.-]*|[0-9]{2,4}[NSEW]B)"


def build_silver_sql():
    """Single CREATE OR REPLACE TABLE AS SELECT for the silver layer."""
    return f"""
CREATE OR REPLACE TABLE {SILVER}
TBLPROPERTIES (delta.enableChangeDataFeed = true,
  comment = 'Silver: rnd_tickets (real + synthetic) + content_hash + light location parse. Key=number; CDF on so the enrich pipeline can stream it. Built by data_generation/build_silver.py.')
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

    The `notes` field is an append-only log, one dated entry per line (verified
    live): 'YYYY-MM-DD [#AUTHOR#|Name|- Name -] text'. Split on newlines, parse the
    leading date + author, keep non-trivial lines.
    """
    return f"""
CREATE OR REPLACE TABLE {NOTE_ENTRIES}
TBLPROPERTIES (delta.enableChangeDataFeed = true,
  comment = 'One row per dated note-log entry per task (key=number+entry_seq). Built by data_generation/build_silver.py.')
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
    """Acceptance assertions; exit non-zero on any failure."""
    print("[silver] --verify: running acceptance assertions...")
    checks = []

    n, st = scalar(f"SELECT count(*) FROM {SILVER}", profile)
    checks.append(("silver row count > 0", n is not None and int(n) > 0, f"{n} (state={st})"))

    n, st = scalar(
        f"SELECT count(*) FROM {SILVER} WHERE content_hash IS NULL OR length(content_hash)=0",
        profile)
    checks.append(("no null/empty content_hash == 0", n is not None and int(n) == 0, f"{n}"))

    n, st = scalar(f"SELECT count(*) - count(DISTINCT number) FROM {SILVER}", profile)
    checks.append(("number is unique (no dupes) == 0", n is not None and int(n) == 0, f"{n}"))

    n, st = scalar(f"SELECT count(*) FROM {NOTE_ENTRIES}", profile)
    checks.append(("note_entries count > 0", n is not None and int(n) > 0, f"{n}"))

    state, rows = run_sql(f"SHOW TBLPROPERTIES {SILVER}", profile, WAREHOUSE_ID)
    cdf_on = any(r and r[0] == "delta.enableChangeDataFeed" and str(r[1]).lower() == "true"
                 for r in (rows or []))
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
    """
    g = globals()
    cat, sch, fq, wh = _env.apply_target_args(args)
    old_fq = g.get("FQ")
    g["CATALOG"], g["SCHEMA"], g["FQ"], g["WAREHOUSE_ID"] = cat, sch, fq, wh
    if old_fq and old_fq != fq:
        for k, v in list(g.items()):
            if isinstance(v, str) and v.startswith(old_fq + "."):
                g[k] = fq + v[len(old_fq):]
    return cat, sch, fq, wh


def main():
    ap = argparse.ArgumentParser(description="Build the R&D silver layer (content_hash gate).")
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true",
                    help="run acceptance assertions after the build")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the build; only run assertions against existing tables")
    args = ap.parse_args()
    _apply_target(args)

    host = assert_target_host(args.profile)  # host gate
    print(f"[silver] Host gate OK: {host}")

    if not args.verify_only:
        run_or_die("build rd_tasks_silver", build_silver_sql(), args.profile)
        run_or_die("build rd_task_note_entries", build_note_entries_sql(), args.profile)

    if args.verify or args.verify_only:
        verify(args.profile)


if __name__ == "__main__":
    main()
