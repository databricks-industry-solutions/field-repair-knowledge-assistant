#!/usr/bin/env python3
"""
Field Repair Knowledge Assistant: land ServiceNow ticket exports in a UC Volume.

Stands in for the ServiceNow export / Lakeflow Connect drop. Each run writes one
newline-delimited JSON file per source bucket into

    /Volumes/<catalog>/<schema>/servicenow_landing/rd_task/

shaped like a ServiceNow Table API export of the `rd_task` table (`sys_id`,
`sys_updated_on`, plus the ticket fields). The `servicenow_ingest` Lakeflow
pipeline (src/pipelines/servicenow_ingest/) picks the files up incrementally with
Auto Loader, so landing the same tickets twice is harmless: Auto CDC keeps the
latest version per ticket.

Reuses parse_tickets.parse_all(), the same deterministic parser behind the
rnd_tickets bronze table, so both ingestion paths see identical tickets.

Usage:
    python3 src/deploy/land_servicenow_exports.py --catalog main --schema troubleshooting_knowledge_agent
"""

import argparse
import io
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so the sibling import resolves either way.
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from parse_tickets import parse_all  # noqa: E402

VOLUME = "servicenow_landing"
SN_TABLE = "rd_task"


def _iso(value):
    """ServiceNow exports timestamps as 'YYYY-MM-DD HH:MM:SS' strings."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d 00:00:00")
    return str(value)


def to_export_record(ticket):
    """One parsed ticket -> one ServiceNow Table API style record."""
    rec = {k: (_iso(v) if isinstance(v, (date, datetime)) else v) for k, v in ticket.items()}
    rec["sys_id"] = ticket["number"]
    rec["sys_class_name"] = SN_TABLE
    rec["sys_updated_on"] = _iso(ticket.get("updated_date") or ticket.get("opened_date"))
    return rec


def main():
    ap = argparse.ArgumentParser(description="Land ServiceNow exports in the landing Volume.")
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--schema", required=True)
    args = ap.parse_args()

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    tickets, _ = parse_all()

    by_bucket = {}
    for t in tickets:
        by_bucket.setdefault(t.get("source_status_bucket") or "unknown", []).append(t)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"/Volumes/{args.catalog}/{args.schema}/{VOLUME}/{SN_TABLE}"
    for bucket, rows in sorted(by_bucket.items()):
        body = "\n".join(json.dumps(to_export_record(r), default=str) for r in rows) + "\n"
        path = f"{base}/{SN_TABLE}_{bucket}_{run_ts}.json"
        w.files.upload(path, io.BytesIO(body.encode("utf-8")), overwrite=True)
        print(f"landed {len(rows):>4} tickets -> {path}")

    print(f"done: {len(tickets)} tickets in {len(by_bucket)} export files")


if __name__ == "__main__":
    main()
