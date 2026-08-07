#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 4 Genie Space builder (Plan 04-03).

Creates (or updates) the "FIS R&D Tickets" Genie Space over BOTH
`rnd_tickets` and `ticket_activity`, and pushes the curated serialized_space
(instructions + example_question_sqls + sql_snippets/measures) from
agents/genie_config.json so the archetype aggregations are correct by
construction (D-05/D-06):

  - INVOLVEMENT (GEN-02): "who is most involved" JOINs ticket_activity and
    COUNT(DISTINCT number) per actor — never GROUP BY assigned_to. The
    curated example-SQL, run directly, ranks Eduardo Cadelina #1 for NM (55).
  - OPEN-TASK RANKING (GEN-04): status IN ('Open','Pending') ORDER BY
    case_age_days DESC, priority ASC.
  - DELAY/COMPLEXITY (GEN-03): surfaces case_age_days / activity_count /
    comment_count / max_inactivity_gap_days.

Build path (D-08): the AI Dev Kit manage_genie MCP tool holds a stale token
this session, so this script drives the public Genie REST API via the
`databricks` CLI on --profile serverless-stable (OAuth, live-proven). The
create endpoint (`POST /api/2.0/genie/spaces`) accepts `serialized_space` as a
JSON string; the serialized_space v2 "export proto" requires:
  * each config/instruction element carries a lowercase 32-hex `id`,
  * every element list is sorted by that `id`,
  * `column_configs` are sorted by `column_name`.
This script injects/sorts those before pushing (the raw agents/genie_config.json
is kept human-editable — ids are ephemeral build detail).

After the space is live it cross-checks the GEN-02 involvement SQL directly on
the warehouse and asserts Eduardo Cadelina ranks #1 for NM, then records the
space_id + cross-check into
.planning/phases/04-knowledge-assistant-genie-space/04-GENIE-BUILD.md
(consumed by plan 04-04 isolation harness + Phase 5 Supervisor attach).

Usage:
    python3 agents/build_genie.py --profile serverless-stable
    python3 agents/build_genie.py --profile serverless-stable --dry-run
"""

import argparse
import json
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# --- Reuse the Phase 1 host-safety gate + SQL runner ------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "preflight"))
from preflight import assert_target_host, run_cli, run_sql  # noqa: E402

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
# ENR-03 repoint target: the curated analytics view (04.1-04), NOT the raw
# rnd_tickets / ticket_activity tables.
ANALYTICS_VIEW = f"{CATALOG}.{SCHEMA}.rd_tasks_gold_analytics"

SPACE_TITLE = "FIS R&D Tickets"
SPACE_DESCRIPTION = (
    "Structured NL->SQL over the curated FIS R&D task analytics view "
    "(rd_tasks_gold_analytics) — one enriched row per task. Answers counts, "
    "durations, expert-finding, priority and site-pattern analysis. Terminology "
    "is category-driven: system terms filter the systems_involved ARRAY, "
    "non-system jargon matches the TEXT columns (no hardcoded term list)."
)

CONFIG_PATH = Path(__file__).resolve().parent / "genie_config.json"
BUILD_DOC = Path(
    ".planning/phases/04-knowledge-assistant-genie-space/04-GENIE-BUILD.md"
)

# ENR-03 correctness proof (replaces the old NM-involvement cross-check): the
# array-vs-text terminology trap. CA is category=software → NOT a systems_involved
# value, so array_contains(systems_involved,'CA') is a FALSE 0; CA must be matched
# as a standalone token in the TEXT columns. ATIS is category=system → still
# counts via array_contains (no regression). Counts are read LIVE (the corpus can
# grow), not hardcoded — the proof is the RELATION (array=0 < text; system term
# nonzero via array), not a fixed number.
_TEXT_COLS = ["title", "summary", "customer_impact", "troubleshooting",
              "recommendation", "root_cause", "resolution"]
# Standalone-token boundary match so 'CA' does not match 'calibration'/'camera'.
_CA_TEXT_PRED = " OR ".join(
    f"{c} RLIKE '(^|[^A-Za-z0-9])CA([^A-Za-z0-9]|$)'" for c in _TEXT_COLS)

CA_ARRAY_SQL = (
    f"SELECT count(*) FROM {ANALYTICS_VIEW} "
    f"WHERE array_contains(systems_involved, 'CA')")
CA_TEXT_SQL = (
    f"SELECT count(*) FROM {ANALYTICS_VIEW} WHERE {_CA_TEXT_PRED}")
ATIS_ARRAY_SQL = (
    f"SELECT count(*) FROM {ANALYTICS_VIEW} "
    f"WHERE array_contains(systems_involved, 'ATIS')")


def _new_id() -> str:
    """Lowercase 32-hex UUID without hyphens (serialized_space id format)."""
    return uuid.uuid4().hex


def prepare_serialized_space(config: dict) -> str:
    """Inject serialized_space `id`s and apply the export-proto sort order.

    The public create endpoint validates the serialized_space "export proto":
      - every config/instruction element must carry a 32-hex `id`,
      - each element list must be sorted by `id`,
      - `column_configs` must be sorted by `column_name`.
    We inject ids here (rather than hardcoding them in the human-editable
    config) and sort so the payload is accepted.
    """

    def inject_and_sort(items):
        if not items:
            return
        for item in items:
            item.setdefault("id", _new_id())
        items.sort(key=lambda x: x["id"])

    cfg = json.loads(json.dumps(config))  # deep copy

    inject_and_sort(cfg.get("config", {}).get("sample_questions", []))

    instr = cfg.get("instructions", {})
    inject_and_sort(instr.get("text_instructions", []))
    inject_and_sort(instr.get("example_question_sqls", []))
    snippets = instr.get("sql_snippets", {})
    inject_and_sort(snippets.get("filters", []))
    inject_and_sort(snippets.get("measures", []))

    # column_configs must be sorted by column_name (no id required there).
    for table in cfg.get("data_sources", {}).get("tables", []):
        ccs = table.get("column_configs")
        if ccs:
            ccs.sort(key=lambda c: c["column_name"])

    return json.dumps(cfg)


def find_existing_space(profile: str, title: str):
    """Return space_id of an existing space with matching title, else None."""
    code, out, _ = run_cli(["api", "get", "/api/2.0/genie/spaces"], profile)
    if code != 0:
        return None
    try:
        spaces = json.loads(out).get("spaces", [])
    except json.JSONDecodeError:
        return None
    for s in spaces:
        if (s.get("title") or "") == title:
            return s.get("space_id")
    return None


def _api_with_json_body(verb: str, path: str, body: dict, profile: str):
    """Run `databricks api <verb> <path> --json @tmpfile`; return (code,out,err)."""
    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False
    ) as fh:
        json.dump(body, fh)
        tmp = fh.name
    try:
        return run_cli(["api", verb, path, "--json", f"@{tmp}"], profile)
    finally:
        Path(tmp).unlink(missing_ok=True)


def create_space(profile: str, serialized_space: str):
    """POST /api/2.0/genie/spaces with the curated serialized_space string."""
    body = {
        "warehouse_id": WAREHOUSE_ID,
        "title": SPACE_TITLE,
        "description": SPACE_DESCRIPTION,
        "serialized_space": serialized_space,
    }
    code, out, err = _api_with_json_body(
        "post", "/api/2.0/genie/spaces", body, profile
    )
    if code != 0:
        print(f"FATAL: space create failed: {err or out}", file=sys.stderr)
        sys.exit(4)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        print(f"FATAL: could not parse create response: {out[:300]}", file=sys.stderr)
        sys.exit(4)


def update_space(profile: str, space_id: str, serialized_space: str):
    """PATCH an existing space with the curated serialized_space string."""
    body = {
        "title": SPACE_TITLE,
        "description": SPACE_DESCRIPTION,
        "serialized_space": serialized_space,
    }
    code, out, err = _api_with_json_body(
        "patch", f"/api/2.0/genie/spaces/{space_id}", body, profile
    )
    if code != 0:
        print(f"FATAL: space update failed: {err or out}", file=sys.stderr)
        sys.exit(4)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"space_id": space_id}


def _scalar(sql: str, profile: str):
    state, data = run_sql(sql, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return int(data[0][0]), state


def cross_check_terminology(profile: str):
    """ENR-03 correctness proof, read LIVE off the analytics view.

    Asserts the array-vs-text terminology fix holds by construction:
      - CA via array_contains(systems_involved,'CA') == 0   (the false 0)
      - CA via TEXT-column match               >  0         (the correct count)
      - ATIS via array_contains               >  0          (system term — no regression)

    Returns (ok, {ca_array, ca_text, atis_array}). The proof is the RELATION, not
    a hardcoded number (the corpus can grow).
    """
    ca_array, s1 = _scalar(CA_ARRAY_SQL, profile)
    ca_text, s2 = _scalar(CA_TEXT_SQL, profile)
    atis_array, s3 = _scalar(ATIS_ARRAY_SQL, profile)
    if None in (ca_array, ca_text, atis_array):
        print(f"FATAL: terminology proof SQL did not succeed "
              f"({s1}/{s2}/{s3})", file=sys.stderr)
        return False, {"ca_array": ca_array, "ca_text": ca_text,
                       "atis_array": atis_array}
    ok = (ca_array == 0 and ca_text > 0 and atis_array > 0)
    return ok, {"ca_array": ca_array, "ca_text": ca_text,
                "atis_array": atis_array}


def write_build_doc(space_id: str, cross_ok: bool, counts: dict, operation: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ca_array = counts.get("ca_array")
    ca_text = counts.get("ca_text")
    atis_array = counts.get("atis_array")
    lines = [
        "# Phase 4.1 — Genie Build Record (repointed at rd_tasks_gold_analytics, ENR-03)",
        "",
        f"**Generated:** {ts}",
        f"**Workspace:** `fevm-serverless-stable-l26d62` (id `7474646739115164`)",
        f"**Warehouse:** `{WAREHOUSE_ID}`",
        "",
        "## Genie Space",
        "",
        f"- **space_id:** `{space_id}`  ← Phase 5 Supervisor + plan 04.1-05 isolation harness consume this",
        f"- **title:** {SPACE_TITLE}",
        f"- **operation:** {operation}",
        f"- **table:** `{ANALYTICS_VIEW}` (curated, COMMENTed serving view — 04.1-04)",
        "- **serialized_space path (confirmed live):** `POST /api/2.0/genie/spaces` "
        "accepts `serialized_space` as a JSON string; the v2 export proto requires "
        "per-element 32-hex `id`s, each element list sorted by `id`, and "
        "`column_configs` sorted by `column_name` (injected/sorted by build_genie.py).",
        "- **curation applied (ENR-03):** category-driven filter rule ONLY — "
        "`systems_involved`/`hardware_mentioned`/`vendors` are arrays, filter a "
        "system term with `array_contains`, match any non-system term in the TEXT "
        "columns with `ILIKE`. NO hardcoded term list, NO state-name→code mapping "
        "(the column comments carry those hints).",
        "",
        "## ENR-03 Correctness Proof (array-vs-text terminology trap)",
        "",
        "Read live off the analytics view:",
        "",
        "| Query | SQL shape | Count | Meaning |",
        "|-------|-----------|-------|---------|",
        f"| CA via `array_contains(systems_involved,'CA')` | array | **{ca_array}** | "
        "the FALSE 0 — CA is category=software, never in the systems array |",
        f"| CA via TEXT-column standalone-token match | ILIKE/RLIKE | **{ca_text}** | "
        "the CORRECT nonzero count (CA matched in title/summary/… text) |",
        f"| ATIS via `array_contains(systems_involved,'ATIS')` | array | **{atis_array}** | "
        "system term still counts via the array — NO regression |",
        "",
        f"- **Proof:** {'PASS' if cross_ok else 'FAIL'} — "
        f"{'CA array=0 < CA text>0 (fix beats the false 0) and ATIS array>0 (no regression).' if cross_ok else 'the array-vs-text relation did NOT hold.'}",
        "- The proof is the RELATION (array-0 for a non-system term vs a nonzero "
        "text count; nonzero array for a system term), not a fixed number — the "
        "corpus can grow. The literal reference count \"CA→3\" was from the 23-real-"
        "ticket reference corpus; this repo's live corpus is 223 rows (23 real + "
        "200 synthetic), so CA text-matches more tasks.",
        "",
        "> GEN-06/07 in `agents/test_genie.py` encode this same proof as a standing "
        "regression test through the live Genie Conversation API.",
        "",
    ]
    BUILD_DOC.parent.mkdir(parents=True, exist_ok=True)
    BUILD_DOC.write_text("\n".join(lines))
    print(f"Wrote {BUILD_DOC}")



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
    ap = argparse.ArgumentParser()
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default="serverless-stable")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config + run the ENR-03 correctness proof without "
        "creating/updating the Genie Space.",
    )
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    # Step 0 — never proceed against the wrong/unauthenticated workspace.
    host = assert_target_host(args.profile)
    print(f"Host gate OK: {host}")

    config = json.loads(CONFIG_PATH.read_text())
    serialized = prepare_serialized_space(config)
    print(f"Prepared serialized_space ({len(serialized)} chars)")

    # ENR-03 correctness proof runs regardless (it's a data assertion).
    cross_ok, counts = cross_check_terminology(args.profile)
    print(f"ENR-03 proof: CA array={counts.get('ca_array')} (want 0), "
          f"CA text={counts.get('ca_text')} (want >0), "
          f"ATIS array={counts.get('atis_array')} (want >0) — "
          f"{'PASS' if cross_ok else 'FAIL'}")
    if not cross_ok:
        print("FATAL: ENR-03 array-vs-text correctness proof failed; refusing to "
              "record a build that contradicts the expected relation.",
              file=sys.stderr)
        sys.exit(5)

    if args.dry_run:
        print("Dry run: skipping space create/update.")
        return

    existing = find_existing_space(args.profile, SPACE_TITLE)
    if existing:
        print(f"Updating existing space {existing}")
        resp = update_space(args.profile, existing, serialized)
        operation = "updated"
        space_id = resp.get("space_id", existing)
    else:
        print("Creating new Genie Space")
        resp = create_space(args.profile, serialized)
        operation = "created"
        space_id = resp.get("space_id", "")

    if not space_id:
        print("FATAL: no space_id returned", file=sys.stderr)
        sys.exit(4)
    print(f"Genie Space {operation}: {space_id}")

    write_build_doc(space_id, cross_ok, counts, operation)
    print(f"DONE. space_id={space_id}")


if __name__ == "__main__":
    main()
