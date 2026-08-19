#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant — Phase 2 Delta loader.

Creates the two canonical Delta tables in the demo schema and loads the 23
parsed real R&D tickets + their actor-events into them via the serverless SQL
path proven in Phase 1 (`preflight.run_sql` + host-assertion gate).

Design (per CONTEXT D-02/D-03/D-05/D-06/D-09 + RESEARCH §Canonical Table Shape,
§KA Delta-Source Validity, §Writing to Delta on Serverless, §Pitfall F):
  - Step 0 host-assertion gate (T-02-03): `assert_target_host` refuses to write
    to any workspace but the reference workspace.
  - Imports `parse_all()` from parse_tickets (the deterministic parser) — the
    loader never re-parses, it only types + escapes + writes.
  - CREATE OR REPLACE TABLE with `delta.enableChangeDataFeed = true` set AT
    CREATE (D-09) so re-runs are idempotent (no accumulation) and the KA
    contract is satisfied without a follow-up ALTER.
  - metadata STRUCT built with EXACTLY the 4 KA-required fields in order/types
    (file_path STRING, file_name STRING, file_size BIGINT,
    file_modification_time TIMESTAMP) via named_struct — business fields stay
    TOP-LEVEL columns (Pitfall D: wrong struct shape fails silently at Phase 4).
  - Free-text (description/notes/close_notes/case_text/detail) single-quote
    escaped by doubling '' (T-02-04 / Pitfall F); multi-line literals are fine.

Usage:
    python3 parse/load_tables.py --profile serverless-stable
"""

import argparse
import subprocess
import time
import json
import sys
from datetime import datetime, date
from pathlib import Path

# Make the repo root importable so both `parse_tickets` and
# `preflight.preflight` resolve regardless of the invocation cwd.
# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally.
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO_ROOT = _HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
SYNTH_DIR = REPO_ROOT / "synth"
if str(SYNTH_DIR) not in sys.path:
    sys.path.insert(0, str(SYNTH_DIR))

from parse_tickets import parse_all  # noqa: E402
from preflight import (  # noqa: E402
    DEMO_CATALOG,
    DEMO_SCHEMA,
    DEFAULT_PROFILE,
    assert_target_host,
    first_warehouse_id,
    run_sql,
)
import env as _env  # noqa: E402

WAREHOUSE_ID = _env.WAREHOUSE_ID
FQ = f"{DEMO_CATALOG}.{DEMO_SCHEMA}"
TICKETS = f"{FQ}.rnd_tickets"
ACTIVITY = f"{FQ}.ticket_activity"

# --- SQL literal builders (escaping — Pitfall F / T-02-04) ------------------


def sql_str(value):
    """A SQL string literal with single-quotes doubled, or NULL."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def sql_int(value):
    """A SQL integer literal, or NULL."""
    if value is None:
        return "NULL"
    return str(int(value))


def sql_ts(value):
    """A SQL TIMESTAMP literal, or a typed NULL (keeps VALUES columns typed)."""
    if value is None:
        return "CAST(NULL AS TIMESTAMP)"
    if isinstance(value, datetime):
        s = value.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(value, date):
        s = value.strftime("%Y-%m-%d 00:00:00")
    else:
        s = str(value)
    return f"TIMESTAMP '{s}'"


def sql_str_array(values):
    """A SQL ARRAY<STRING> literal, or a typed empty array."""
    if not values:
        return "CAST(array() AS ARRAY<STRING>)"
    inner = ", ".join(sql_str(v) for v in values)
    return f"array({inner})"


def sql_metadata(number, case_text, updated_date):
    """named_struct with EXACTLY the 4 KA-required fields (order + types).

    file_size is CAST to BIGINT (LONG) — the exact type KA requires; an INT
    here is the silent Phase-4 attach failure (Pitfall D).
    """
    file_name = f"{number}.md"
    file_path = f"/synthetic/{number}.md"
    file_size = len(case_text) if case_text is not None else 0
    return (
        "named_struct("
        f"'file_path', {sql_str(file_path)}, "
        f"'file_name', {sql_str(file_name)}, "
        f"'file_size', CAST({file_size} AS BIGINT), "
        f"'file_modification_time', {sql_ts(updated_date)}"
        ")"
    )


# --- DDL --------------------------------------------------------------------

CREATE_TICKETS = f"""
CREATE OR REPLACE TABLE {TICKETS} (
  number STRING,
  title STRING,
  parent STRING,
  assignment_group STRING,
  assigned_to STRING,
  priority INT,
  priority_label STRING,
  status STRING,
  workflow_status STRING,
  follow_up STRING,
  location STRING,
  opened_by STRING,
  opened_date TIMESTAMP,
  updated_date TIMESTAMP,
  closed_date TIMESTAMP,
  description STRING,
  notes STRING,
  close_notes STRING,
  case_text STRING,
  involved_users ARRAY<STRING>,
  case_age_days INT,
  activity_count INT,
  comment_count INT,
  max_inactivity_gap_days INT,
  metadata STRUCT<file_path:STRING, file_name:STRING, file_size:BIGINT, file_modification_time:TIMESTAMP>,
  source_status_bucket STRING
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""".strip()

CREATE_ACTIVITY = f"""
CREATE OR REPLACE TABLE {ACTIVITY} (
  number STRING,
  actor STRING,
  event_type STRING,
  event_ts TIMESTAMP,
  detail STRING
) USING DELTA
TBLPROPERTIES (delta.enableChangeDataFeed = true)
""".strip()


# --- Row builders -----------------------------------------------------------

def ticket_values(t, with_is_synthetic=False):
    """Build one VALUES tuple for rnd_tickets (column order matches DDL).

    When `with_is_synthetic` is True the tuple gains a trailing BOOLEAN column
    matching the appended `is_synthetic` column (append path only — the real
    23-row create path never sets it, so those rows are backfilled to false).
    """
    inner = (
        f"{sql_str(t['number'])}, "
        f"{sql_str(t['title'])}, "
        f"{sql_str(t['parent'])}, "
        f"{sql_str(t['assignment_group'])}, "
        f"{sql_str(t['assigned_to'])}, "
        f"{sql_int(t['priority'])}, "
        f"{sql_str(t['priority_label'])}, "
        f"{sql_str(t['status'])}, "
        f"{sql_str(t['workflow_status'])}, "
        f"{sql_str(t['follow_up'])}, "
        f"{sql_str(t['location'])}, "
        f"{sql_str(t['opened_by'])}, "
        f"{sql_ts(t['opened_date'])}, "
        f"{sql_ts(t['updated_date'])}, "
        f"{sql_ts(t['closed_date'])}, "
        f"{sql_str(t['description'])}, "
        f"{sql_str(t['notes'])}, "
        f"{sql_str(t['close_notes'])}, "
        f"{sql_str(t['case_text'])}, "
        f"{sql_str_array(t['involved_users'])}, "
        f"{sql_int(t['case_age_days'])}, "
        f"{sql_int(t['activity_count'])}, "
        f"{sql_int(t['comment_count'])}, "
        f"{sql_int(t['max_inactivity_gap_days'])}, "
        f"{sql_metadata(t['number'], t['case_text'], t['updated_date'])}, "
        f"{sql_str(t['source_status_bucket'])}"
    )
    if with_is_synthetic:
        val = "TRUE" if t.get("is_synthetic") else "FALSE"
        inner += f", {val}"
    return f"({inner})"


def activity_values(a):
    """Build one VALUES tuple for ticket_activity (column order matches DDL)."""
    return (
        "("
        f"{sql_str(a['number'])}, "
        f"{sql_str(a['actor'])}, "
        f"{sql_str(a['event_type'])}, "
        f"{sql_ts(a['event_ts'])}, "
        f"{sql_str(a['detail'])}"
        ")"
    )


# --- Execution --------------------------------------------------------------

def exec_sql(stmt, profile, warehouse_id, label):
    """Run a statement, fail loudly (never a silent partial load)."""
    state, data = run_sql(stmt, profile, warehouse_id)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} did not succeed (state={state}).", file=sys.stderr)
        print(f"  statement head: {stmt[:200]}", file=sys.stderr)
        sys.exit(4)
    return data


def exec_sql_poll(stmt, profile, warehouse_id, label, poll_timeout=1200):
    """Run a long statement async + poll to a terminal state (batched INSERTs).

    A large multi-row INSERT can exceed the 50s synchronous wait window that
    `run_sql` uses (it would return PENDING). This submits with wait_timeout=0s
    and GETs the statement id until terminal. Fails loudly on non-SUCCEEDED.
    """
    payload = json.dumps({
        "warehouse_id": warehouse_id,
        "statement": stmt,
        "wait_timeout": "0s",
    })
    p = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", payload, "--profile", profile],
        capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        print(f"FATAL: {label} submit failed: {p.stderr[:200]}", file=sys.stderr)
        sys.exit(4)
    d = json.loads(p.stdout)
    statement_id = d.get("statement_id")
    state = d.get("status", {}).get("state", "UNKNOWN")
    deadline = time.time() + poll_timeout
    while state in ("PENDING", "RUNNING") and time.time() < deadline:
        time.sleep(3)
        g = subprocess.run(
            ["databricks", "api", "get",
             f"/api/2.0/sql/statements/{statement_id}", "--profile", profile],
            capture_output=True, text=True, timeout=180)
        if g.returncode != 0:
            print(f"FATAL: {label} poll failed: {g.stderr[:200]}",
                  file=sys.stderr)
            sys.exit(4)
        d = json.loads(g.stdout)
        state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        err = d.get("status", {}).get("error", {}).get("message", "")
        print(f"FATAL: {label} did not succeed (state={state}). {err[:300]}",
              file=sys.stderr)
        print(f"  statement head: {stmt[:200]}", file=sys.stderr)
        sys.exit(4)
    return d.get("result", {}).get("data_array")


# --- Append mode (Wave 3 — D-10/D-11, INSERT-only, never CREATE OR REPLACE) --

def has_column(table, column, profile, warehouse_id):
    """True iff `column` already exists on `table` (DESCRIBE-based idempotency)."""
    state, data = run_sql(f"DESCRIBE {table}", profile, warehouse_id)
    if state != "SUCCEEDED":
        print(f"FATAL: DESCRIBE {table} failed (state={state}) — cannot append "
              f"to a table that does not exist. Run the Phase-2 load first.",
              file=sys.stderr)
        sys.exit(4)
    cols = {str(r[0]).strip().lower() for r in (data or []) if r}
    return column.lower() in cols


def append_synthetic(profile, warehouse_id, host):
    """Leakage-gated, INSERT-only, idempotent synthetic append (D-10/D-11).

    Sequence (all after assert_target_host, which the caller already ran):
      1. Post-process the staged synthetic records + run the Tier-1 leakage
         gate. Any hard fail ABORTS before any write.
      2. One-time DDL: ALTER ADD COLUMN is_synthetic on rnd_tickets (skip if
         present) + explicit UPDATE backfill of existing (real) rows to false.
      3. Idempotent append: DELETE synthetic rows, then batched INSERT of the
         200 synthetic tickets (is_synthetic=true) + their activity events.
      4. Post-append assertions: total >= 200, is_synthetic=false == 23,
         is_synthetic IS NULL == 0, no 0001xxx among synthetic.
    Never CREATE OR REPLACE the real corpus (Pitfall 4 — that wipes the 23).
    """
    from postprocess import process_all  # local import: only append needs it
    from leakage_gate import check_leakage, load_answer_sentences

    # 1. Post-process + HARD leakage gate BEFORE any write.
    tickets, activities, skipped = process_all(profile, warehouse_id)
    print(f"Post-processed {len(tickets)} synthetic tickets + "
          f"{len(activities)} activity events ({len(skipped)} skipped) on {host}")

    ok, hard_fails, warns = check_leakage(tickets, load_answer_sentences())
    if not ok:
        print(f"FATAL: Tier-1 leakage gate RED — {len(hard_fails)} hit(s); "
              f"the append is ABORTED (no write performed).", file=sys.stderr)
        for number, kind, matched in hard_fails[:20]:
            print(f"  └─ {number} [{kind}] {matched}", file=sys.stderr)
        sys.exit(5)
    print(f"Tier-1 leakage gate GREEN (0 hits; {len(warns)} Tier-2 warns) — "
          f"proceeding to append.")

    if not tickets:
        print("FATAL: 0 synthetic tickets to append.", file=sys.stderr)
        sys.exit(5)

    # 2. One-time DDL (idempotent) + explicit backfill of the real rows.
    if not has_column(TICKETS, "is_synthetic", profile, warehouse_id):
        exec_sql(f"ALTER TABLE {TICKETS} ADD COLUMN is_synthetic BOOLEAN",
                 profile, warehouse_id, "ALTER ADD COLUMN is_synthetic")
        print(f"Added is_synthetic column to {TICKETS}")
    else:
        print(f"is_synthetic column already present on {TICKETS} (skip ALTER)")
    # ADD COLUMN leaves existing rows NULL — explicit backfill (never a DEFAULT).
    exec_sql(f"UPDATE {TICKETS} SET is_synthetic = false WHERE is_synthetic IS NULL",
             profile, warehouse_id, "BACKFILL is_synthetic=false on real rows")

    # 3. Idempotent append (delete-then-insert; never touches real rows).
    #    ticket_activity has no is_synthetic column — synthetic events are keyed
    #    by the synthetic number range (R&DTASK0002%), so delete on that range.
    exec_sql(f"DELETE FROM {TICKETS} WHERE is_synthetic = true",
             profile, warehouse_id, "DELETE prior synthetic tickets")
    exec_sql(f"DELETE FROM {ACTIVITY} WHERE number LIKE 'R&DTASK0002%'",
             profile, warehouse_id, "DELETE prior synthetic activities")

    BATCH = 100
    for i in range(0, len(tickets), BATCH):
        chunk = tickets[i:i + BATCH]
        rows = ",\n".join(ticket_values(t, with_is_synthetic=True) for t in chunk)
        exec_sql_poll(f"INSERT INTO {TICKETS} VALUES\n{rows}",
                      profile, warehouse_id,
                      f"INSERT synthetic rnd_tickets[{i}:{i+len(chunk)}]")
    for i in range(0, len(activities), BATCH):
        chunk = activities[i:i + BATCH]
        rows = ",\n".join(activity_values(a) for a in chunk)
        exec_sql_poll(f"INSERT INTO {ACTIVITY} VALUES\n{rows}",
                      profile, warehouse_id,
                      f"INSERT synthetic ticket_activity[{i}:{i+len(chunk)}]")

    # 4. Post-append assertions (the D-10 integrity guarantees).
    def _scalar(stmt):
        _, d = run_sql(stmt, profile, warehouse_id)
        return int(d[0][0]) if d and d[0] and d[0][0] is not None else None

    total = _scalar(f"SELECT COUNT(*) FROM {TICKETS}")
    real = _scalar(f"SELECT COUNT(*) FROM {TICKETS} WHERE is_synthetic = false")
    nulls = _scalar(f"SELECT COUNT(*) FROM {TICKETS} WHERE is_synthetic IS NULL")
    collide = _scalar(f"SELECT COUNT(*) FROM {TICKETS} "
                      f"WHERE is_synthetic = true AND number LIKE 'R&DTASK0001%'")
    print(f"\nPost-append: total={total} (need >=200), is_synthetic=false={real} "
          f"(need 23), is_synthetic NULL={nulls} (need 0), synthetic in "
          f"0001xxx range={collide} (need 0)")
    if not (total is not None and total >= 200 and real == 23
            and nulls == 0 and collide == 0):
        print("FATAL: post-append integrity assertion failed — the 23 real rows "
              "or the count/collision guarantees are violated.", file=sys.stderr)
        sys.exit(6)
    print(f"Append COMPLETE — corpus is {total} rows, 23 real intact "
          f"(is_synthetic=false), synthetic INSERT-only + idempotent.")


def _apply_target(args):
    """Rebind FQ/TICKETS/ACTIVITY/WAREHOUSE_ID from --catalog/--schema/--warehouse-id.

    Serverless job environments cannot set env vars, so the flags are the only way a
    DAB task can retarget this script (mirrors build_silver._apply_target).
    """
    g = globals()
    cat, sch, fq, wh = _env.apply_target_args(args)
    old_fq = g.get("FQ")
    g["FQ"], g["WAREHOUSE_ID"] = fq, wh
    if old_fq and old_fq != fq:
        # Replace the old FQ ANYWHERE it appears in a string global — not just as a
        # prefix — so module-level SQL constants (CREATE_TICKETS/CREATE_ACTIVITY, which
        # embed "<catalog>.<schema>.rnd_tickets" inline) are retargeted too.
        needle = old_fq + "."
        for k, v in list(g.items()):
            if isinstance(v, str) and needle in v:
                g[k] = v.replace(needle, fq + ".")
    return cat, sch, fq, wh


def main():
    ap = argparse.ArgumentParser()
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--append-synthetic", action="store_true",
                    help="Wave-3 append mode: leakage-gate + ALTER ADD "
                         "is_synthetic + backfill + INSERT-only idempotent "
                         "append of the staged synthetic rows. Never touches "
                         "the real-corpus CREATE OR REPLACE path.")
    args = ap.parse_args()
    _apply_target(args)

    # Step 0 — never write to the wrong/unauthenticated workspace (T-02-03).
    host = assert_target_host(args.profile)
    # Warehouse comes from --warehouse-id (DAB passes ${var.warehouse_id}); fall back
    # to auto-discovery only when it was not provided (local convenience).
    warehouse_id = WAREHOUSE_ID or first_warehouse_id(args.profile)
    if not warehouse_id:
        print("FATAL: no serverless warehouse resolved; pass --warehouse-id.",
              file=sys.stderr)
        sys.exit(2)

    # Append mode: INSERT-only extension of the EXISTING corpus (D-10). The
    # CREATE OR REPLACE path below is NEVER reached — it would wipe the 23 real
    # rows (Pitfall 4).
    if args.append_synthetic:
        append_synthetic(args.profile, warehouse_id, host)
        return

    tickets, activities = parse_all()
    print(f"Parsed {len(tickets)} tickets + {len(activities)} actor-events; "
          f"loading into {FQ} on {host}")

    # 1. Schema.
    exec_sql(f"CREATE SCHEMA IF NOT EXISTS {FQ}", args.profile, warehouse_id,
             "CREATE SCHEMA")

    # 2. Tables (CDF at create; CREATE OR REPLACE → idempotent re-runs).
    exec_sql(CREATE_TICKETS, args.profile, warehouse_id, "CREATE rnd_tickets")
    exec_sql(CREATE_ACTIVITY, args.profile, warehouse_id, "CREATE ticket_activity")

    # 3. Load rnd_tickets (23 rows — one batched INSERT).
    ticket_rows = ",\n".join(ticket_values(t) for t in tickets)
    exec_sql(f"INSERT INTO {TICKETS} VALUES\n{ticket_rows}",
             args.profile, warehouse_id, "INSERT rnd_tickets")

    # 4. Load ticket_activity (batched to keep each statement well under limits).
    BATCH = 200
    for i in range(0, len(activities), BATCH):
        chunk = activities[i:i + BATCH]
        rows = ",\n".join(activity_values(a) for a in chunk)
        exec_sql(f"INSERT INTO {ACTIVITY} VALUES\n{rows}",
                 args.profile, warehouse_id, f"INSERT ticket_activity[{i}:{i+len(chunk)}]")

    # 5. Confirm counts.
    _, tc = run_sql(f"SELECT COUNT(*) FROM {TICKETS}", args.profile, warehouse_id)
    _, ac = run_sql(f"SELECT COUNT(*) FROM {ACTIVITY}", args.profile, warehouse_id)
    ticket_count = tc[0][0] if tc else "?"
    activity_count = ac[0][0] if ac else "?"
    print(f"Loaded: rnd_tickets={ticket_count} rows, "
          f"ticket_activity={activity_count} rows")


if __name__ == "__main__":
    main()
