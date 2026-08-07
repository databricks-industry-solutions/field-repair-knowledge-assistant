#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 4.1 Plan 04: silver → gold enrichment (ENR-01,
ENR-02, GLO-02).

Ports the reference `/tmp/servicenow_demo/notebooks/40_enrich.py` into this repo's
CLI-driven, host-gated harness (mirrors silver.py / build_glossary.py). No Spark
session — every stage is a SQL statement issued through `run_sql` / `run_sql_poll`
against warehouse 04a4dee7888b9e64.

One `ai_query('databricks-claude-sonnet-4-5', …, responseFormat => json_schema)`
statement enriches every silver row not already present in gold (content_hash
LEFT ANTI JOIN → incremental). Structured output yields the canonical columns:
  systems_involved[] (enum), hardware_mentioned[] (free), vendors[] (enum),
  problem_category (enum), the 4 meaning-segmented description parts
  (summary/customer_impact/troubleshooting/recommendation — empty string, never
  invented, where the ticket does not cover a part — ENR-02), root_cause,
  resolution, resolution_type (enum), plus per-field confidence.

GLO-02 coupling: the systems/vendors ENUMs are built at RUN TIME from the approved
glossary (`SELECT term FROM ...glossary WHERE status='approved' AND category='system'`
and `='vendor'`) and injected into the responseFormat json_schema `items.enum` AND
the system prompt. There is NO hardcoded system/vendor list anywhere. `--drift-guard`
proves the enrichment array values and the glossary category=system set stay
identical (EXCEPT both directions).

Pipeline (materialize-once — never reference an ai_query column twice, Pitfall 5):
  rd_tasks_gold_enrichment   — CREATE IF NOT EXISTS so the first-run anti-join works
  _gold_enrich_stage         — todo (anti-join) → ai_query → from_json parse (materialized)
  rd_tasks_gold_enrichment   — MERGE INTO ... ON number (idempotent, content_hash-gated)
  rd_tasks_gold              — VIEW = silver ⋈ enrichment (+ note-entry count)
  rd_tasks_gold_analytics    — curated, COMMENTed serving view for Genie (Wave 3)

Usage:
    python3 enrich/enrich.py --profile serverless-stable
    python3 enrich/enrich.py --profile serverless-stable --verify
    python3 enrich/enrich.py --profile serverless-stable --drift-guard
"""

import argparse
import json
import re
import sys
from pathlib import Path

# --- Reuse the Phase 1 host-safety gate + SQL runners -----------------------
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preflight"))
from preflight import assert_target_host, run_sql  # noqa: E402
# run_sql_poll lives in build_glossary (statement_id polling for long ai_query batches).
sys.path.insert(0, str(REPO / "enrich"))
from build_glossary import run_sql_poll  # noqa: E402

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
T_SILVER = f"{FQ}.rd_tasks_silver"
T_NOTE_ENTRIES = f"{FQ}.rd_task_note_entries"
T_GLOSSARY = f"{FQ}.glossary"
T_GOLD_ENRICH = f"{FQ}.rd_tasks_gold_enrichment"
T_STAGE = f"{FQ}._gold_enrich_stage"
V_GOLD = f"{FQ}.rd_tasks_gold"
V_ANALYTICS = f"{FQ}.rd_tasks_gold_analytics"
DEFAULT_PROFILE = "serverless-stable"

# ai_query BATCH-capable endpoint. sonnet-5 is NOT batch-supported (Pitfall 2);
# sonnet-4-5 verified batch-capable on l26d62.
CHAT_ENDPOINT = "databricks-claude-sonnet-4-5"
PROMPT_VERSION = "enrich-v3-desc-segmented"
CONF_THRESHOLD = 0.6  # min_confidence < 0.6 → needs_review

# Enum sets for non-glossary-coupled fields (fixed taxonomies, not domain terms).
PROBLEM_CATEGORY_ENUM = [
    "hardware_failure", "software_crash", "network_connectivity", "calibration",
    "image_quality", "power", "configuration", "other",
]
RESOLUTION_TYPE_ENUM = [
    "hardware_replace", "software_patch", "recalibration", "config_change", "rma",
    "firmware_update", "monitoring", "no_fix_found", "unresolved", "not_applicable",
]

# The parsed struct (from_json target) — mirrors the responseFormat properties.
PARSE_STRUCT = (
    "STRUCT<systems_involved:ARRAY<STRING>, hardware_mentioned:ARRAY<STRING>, "
    "vendors:ARRAY<STRING>, problem_category:STRING, summary:STRING, "
    "customer_impact:STRING, troubleshooting:STRING, recommendation:STRING, "
    "root_cause:STRING, resolution:STRING, resolution_type:STRING, "
    "conf_systems:DOUBLE, conf_root_cause:DOUBLE, conf_resolution_type:DOUBLE>"
)


# --- gold enrichment table DDL (schema so first-run anti-join works) --------

def sql_gold_table():
    return f"""
CREATE TABLE IF NOT EXISTS {T_GOLD_ENRICH} (
  number STRING, content_hash STRING,
  systems_involved ARRAY<STRING>, hardware_mentioned ARRAY<STRING>, vendors ARRAY<STRING>,
  problem_category STRING,
  summary STRING, customer_impact STRING, troubleshooting STRING, recommendation STRING,
  root_cause STRING, resolution STRING, resolution_type STRING,
  conf_systems DOUBLE, conf_root_cause DOUBLE, conf_resolution_type DOUBLE,
  min_confidence DOUBLE, needs_review BOOLEAN,
  prompt_version STRING, model STRING, enriched_at TIMESTAMP)
COMMENT 'LLM enrichment of R&D tasks via ai_query, gated by content_hash. Joins silver on number.'
""".strip()


# --- Build controlled vocab from the APPROVED glossary (GLO-02, run time) ---

def load_glossary_vocab(profile):
    """Build systems/vendors enums + acronym map FROM the approved glossary.

    NO hardcoded system/vendor list — this is the GLO-02 coupling: a term is an
    enum value iff its glossary category says so.
    """
    st, rows = run_sql(
        f"SELECT term, category, definition FROM {T_GLOSSARY} WHERE status='approved'",
        profile, WAREHOUSE_ID)
    if st != "SUCCEEDED" or rows is None:
        print(f"FATAL: could not load approved glossary (state={st}).", file=sys.stderr)
        sys.exit(4)
    systems, vendors, acr = set(), set(), {}
    for term, category, definition in rows:
        head = (term or "").split(" / ")[0]
        if category == "system":
            systems.add(head)
        elif category == "vendor":
            vendors.add(head)
        # acronym → short definition (for the normalization hint block)
        if re.fullmatch(r"[A-Z0-9]{2,6}", head or ""):
            acr[term] = (definition or "").split(".")[0][:70]
    return sorted(systems), sorted(vendors), acr


def build_response_format(systems, vendors):
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "rd_enrichment",
            "schema": {
                "type": "object",
                "properties": {
                    "systems_involved": {"type": "array", "items": {"type": "string", "enum": systems}},
                    "hardware_mentioned": {"type": "array", "items": {"type": "string"}},
                    "vendors": {"type": "array", "items": {"type": "string", "enum": vendors}},
                    "problem_category": {"type": "string", "enum": PROBLEM_CATEGORY_ENUM},
                    # description de-blobbed into its 4 intents by MEANING, "" where absent.
                    "summary": {"type": "string"},
                    "customer_impact": {"type": "string"},
                    "troubleshooting": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "resolution": {"type": "string"},
                    "resolution_type": {"type": "string", "enum": RESOLUTION_TYPE_ENUM},
                    "conf_systems": {"type": "number"},
                    "conf_root_cause": {"type": "number"},
                    "conf_resolution_type": {"type": "number"},
                },
                "required": [
                    "systems_involved", "problem_category",
                    "summary", "customer_impact", "troubleshooting", "recommendation",
                    "root_cause", "resolution",
                    "resolution_type", "conf_systems", "conf_root_cause", "conf_resolution_type",
                ],
            },
            "strict": True,
        },
    }


def build_system_prompt(systems, vendors, acr):
    acr_block = "; ".join(f"{k} = {v}" for k, v in sorted(acr.items()))
    prompt = (
        "You enrich Fleetworthy roadside truck-screening R&D tickets (weigh-in-motion, "
        "license-plate/DOT reading, cameras at highway inspection sites) into structured data. "
        "Use ONLY these systems: " + json.dumps(systems) + ". Use ONLY these vendors: "
        + json.dumps(vendors) + ". "
        "If a system/vendor is not in those lists, put it in hardware_mentioned. "
        "Domain acronyms: " + acr_block + ". "
        "Segment the description into four parts by MEANING (not by any numbering the "
        "ticket may or may not use): summary (what the issue is), customer_impact (effect "
        "on the customer/site), troubleshooting (steps already taken to verify or resolve), "
        "recommendation (proposed fix / parts). Return an empty string for any part the "
        "ticket does not cover — do NOT invent content. "
        "Be conservative on confidence; use 'unresolved' when the ticket isn't closed with a fix."
    )
    # Avoid single-quote clashes inside the SQL literal (mirrors 40_enrich.py).
    return prompt.replace("'", "’")


# --- Enrichment staging: todo anti-join → ai_query → from_json (materialize) -

def sql_enrich_stage(systems, vendors, acr):
    rf_json = json.dumps(build_response_format(systems, vendors)).replace("'", "\\'")
    sys_prompt_sql = build_system_prompt(systems, vendors, acr).replace("'", "\\'")
    return f"""
CREATE OR REPLACE TABLE {T_STAGE} AS
WITH todo AS (
  SELECT s.number, s.content_hash, s.title, s.description, s.notes, s.close_notes
  FROM {T_SILVER} s
  LEFT ANTI JOIN {T_GOLD_ENRICH} g ON s.content_hash = g.content_hash
),
scored AS (
  SELECT number, content_hash,
    ai_query(
      '{CHAT_ENDPOINT}',
      concat(
        '{sys_prompt_sql}',
        '\\n\\nTASK ', number, ': ', coalesce(title,''),
        '\\n\\nDESCRIPTION:\\n', coalesce(description,'(none)'),
        '\\n\\nNOTES:\\n', coalesce(notes,'(none)'),
        '\\n\\nCLOSE NOTES:\\n', coalesce(close_notes,'(none)')
      ),
      responseFormat => '{rf_json}'
    ) AS js
  FROM todo
),
parsed AS (
  SELECT number, content_hash, from_json(js, '{PARSE_STRUCT}') AS e
  FROM scored
),
-- Normalize confidence to 0..1: the model is inconsistent and returns some fields
-- on a 0..100 percentage scale (verified live: 218/223 rows >1). Any value >1 is
-- divided by 100 so the CONF_THRESHOLD routing is meaningful (Rule-1 fix).
normed AS (
  SELECT number, content_hash, e,
    CASE WHEN e.conf_systems > 1 THEN e.conf_systems/100 ELSE e.conf_systems END AS c_sys,
    CASE WHEN e.conf_root_cause > 1 THEN e.conf_root_cause/100 ELSE e.conf_root_cause END AS c_rc,
    CASE WHEN e.conf_resolution_type > 1 THEN e.conf_resolution_type/100 ELSE e.conf_resolution_type END AS c_rt
  FROM parsed
)
SELECT number, content_hash,
  e.systems_involved, e.hardware_mentioned, e.vendors, e.problem_category,
  e.summary, e.customer_impact, e.troubleshooting, e.recommendation,
  e.root_cause, e.resolution, e.resolution_type,
  c_sys AS conf_systems, c_rc AS conf_root_cause, c_rt AS conf_resolution_type,
  least(c_sys, c_rc, c_rt)                                                  AS min_confidence,
  (least(c_sys, c_rc, c_rt) < {CONF_THRESHOLD})                            AS needs_review,
  '{PROMPT_VERSION}'                                                        AS prompt_version,
  '{CHAT_ENDPOINT}'                                                         AS model,
  current_timestamp()                                                       AS enriched_at
FROM normed
""".strip()


def sql_merge():
    return f"""
MERGE INTO {T_GOLD_ENRICH} t USING {T_STAGE} s ON t.number = s.number
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""".strip()


# --- gold VIEW = silver ⋈ enrichment (+ note-entry count) -------------------

def sql_gold_view():
    return f"""
CREATE OR REPLACE VIEW {V_GOLD} AS
SELECT s.*,
       coalesce(ne.num_note_entries, 0)                                     AS num_note_entries,
       e.systems_involved, e.hardware_mentioned, e.vendors, e.problem_category,
       e.summary, e.customer_impact, e.troubleshooting, e.recommendation,
       e.root_cause, e.resolution, e.resolution_type,
       e.conf_systems, e.conf_root_cause, e.conf_resolution_type,
       e.min_confidence, e.needs_review,
       e.prompt_version, e.model, e.enriched_at
FROM {T_SILVER} s
LEFT JOIN {T_GOLD_ENRICH} e ON s.number = e.number
LEFT JOIN (SELECT number, count(*) AS num_note_entries FROM {T_NOTE_ENTRIES} GROUP BY number) ne
  ON s.number = ne.number
""".strip()


# --- Curated analytics serving view for Genie (Wave 3) ----------------------
# Column COMMENTs ported verbatim from 40_enrich.py (lines 208-250) — they are
# Genie's text-to-SQL hints. Provenance/confidence/content_hash EXCLUDED (noise).
# Column names mapped to this repo's silver columns; the reference's
# first_event/last_event/num_reassignments are not materialized in this repo's
# silver, so they are omitted (not invented).

def sql_analytics_view():
    return f"""
CREATE OR REPLACE VIEW {V_ANALYTICS} (
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
COMMENT 'Curated R&D task analytics for Fleetworthy roadside truck-screening (WIM/ALPR/AUR/ATIS). One row per task, enriched with systems/root-cause/resolution + segmented description. Use for counts, durations, expert-finding, priority and site-pattern analysis.'
WITH SCHEMA COMPENSATION
AS SELECT number, title, parent, assigned_to, priority_level, priority_label, status,
  workflow_status, is_closed, location_state, location_highway, location_site, site_key,
  duration_days, num_note_entries, num_activities,
  systems_involved, hardware_mentioned, vendors, problem_category,
  summary, customer_impact, troubleshooting, recommendation,
  root_cause, resolution, resolution_type, needs_review
FROM {V_GOLD}
""".strip()


# --- runners ----------------------------------------------------------------

def run_or_die(label, stmt, profile, poll=False):
    runner = run_sql_poll if poll else run_sql
    state, data = runner(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} failed (state={state}).", file=sys.stderr)
        print(stmt[:2000], file=sys.stderr)
        sys.exit(4)
    print(f"[enrich] {label}: SUCCEEDED")
    return data


def scalar(stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return data[0][0], state


def enrich(profile):
    print("[enrich] Step 1: ensure gold enrichment table exists (schema)...")
    run_or_die("rd_tasks_gold_enrichment (schema)", sql_gold_table(), profile)

    print("[enrich] Step 2: build systems/vendors enums FROM approved glossary (GLO-02)...")
    systems, vendors, acr = load_glossary_vocab(profile)
    print(f"[enrich]   vocab: {len(systems)} systems {systems}, "
          f"{len(vendors)} vendors {vendors}, {len(acr)} acronyms")
    if not systems:
        print("FATAL: no approved category=system terms — cannot build enum (GLO-02).",
              file=sys.stderr)
        sys.exit(4)

    print("[enrich] Step 3: ai_query enrichment over silver LEFT ANTI JOIN gold (materialize)...")
    run_or_die("_gold_enrich_stage (ai_query)", sql_enrich_stage(systems, vendors, acr),
               profile, poll=True)

    n, _ = scalar(f"SELECT count(*) FROM {T_STAGE}", profile)
    n = int(n) if n is not None else 0
    print(f"[enrich] enriched {n} new/changed task(s)")

    if n:
        print("[enrich] Step 4: MERGE INTO rd_tasks_gold_enrichment ON number...")
        run_or_die("MERGE gold", sql_merge(), profile, poll=True)
    else:
        print("[enrich] Step 4: MERGE skipped (0 new/changed tasks — incremental gate held).")

    print("[enrich] Step 5: rd_tasks_gold view (silver ⋈ enrichment)...")
    run_or_die("rd_tasks_gold view", sql_gold_view(), profile)

    print("[enrich] Step 6: rd_tasks_gold_analytics curated view (Genie serving surface)...")
    run_or_die("rd_tasks_gold_analytics view", sql_analytics_view(), profile)


# --- --verify: acceptance assertions ----------------------------------------

def verify(profile):
    print("[enrich] --verify: running acceptance assertions...")
    checks = []

    n, _ = scalar(f"SELECT count(*) FROM {T_GOLD_ENRICH}", profile)
    checks.append(("gold enrichment count == 223", n is not None and int(n) == 223, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GOLD_ENRICH} WHERE systems_involved IS NULL "
        f"OR problem_category IS NULL OR root_cause IS NULL OR resolution_type IS NULL", profile)
    checks.append(("canonical cols non-null == 0", n is not None and int(n) == 0, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GOLD_ENRICH} WHERE summary IS NULL OR customer_impact IS NULL "
        f"OR troubleshooting IS NULL OR recommendation IS NULL", profile)
    checks.append(("segments never null (ENR-02) == 0", n is not None and int(n) == 0, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GOLD_ENRICH} WHERE model IS NULL OR enriched_at IS NULL "
        f"OR content_hash IS NULL", profile)
    checks.append(("provenance non-null == 0", n is not None and int(n) == 0, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GOLD_ENRICH} WHERE min_confidence > 1 OR min_confidence < 0", profile)
    checks.append(("confidence normalized to 0..1 == 0", n is not None and int(n) == 0, f"{n}"))

    n, _ = scalar(f"SELECT count(*) FROM {V_ANALYTICS}", profile)
    checks.append(("analytics view count == 223", n is not None and int(n) == 223, f"{n}"))

    # analytics view excludes noise columns
    state, rows = run_sql(f"DESCRIBE {V_ANALYTICS}", profile, WAREHOUSE_ID)
    cols = {r[0] for r in rows} if rows else set()
    noise = {"content_hash", "prompt_version", "model", "conf_systems",
             "conf_root_cause", "conf_resolution_type", "min_confidence"}
    checks.append(("analytics excludes provenance/confidence noise",
                   cols.isdisjoint(noise), str(sorted(cols & noise))))
    checks.append(("analytics has systems_involved + segment cols (Genie hints)",
                   {"systems_involved", "summary", "customer_impact",
                    "troubleshooting", "recommendation"} <= cols, ""))

    print("\n[enrich] Acceptance results:")
    all_ok = True
    for label, ok, ev in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  (observed: {ev})")
        all_ok = all_ok and ok
    if not all_ok:
        print("[enrich] VERIFY FAILED", file=sys.stderr)
        sys.exit(5)
    print("[enrich] VERIFY PASSED")


# --- --drift-guard: GLO-02 enum ⟷ glossary coupling (EXCEPT both ways) -------

def drift_guard(profile):
    print("[enrich] --drift-guard: GLO-02 systems enum ⟷ glossary coupling...")
    array_vals = (f"SELECT DISTINCT explode(systems_involved) AS term FROM {T_GOLD_ENRICH} "
                  f"WHERE systems_involved IS NOT NULL")
    gloss_vals = (f"SELECT term FROM {T_GLOSSARY} WHERE status='approved' AND category='system'")

    # Direction A: enrichment array values NOT in the glossary system set.
    st, rows_a = run_sql(f"({array_vals}) EXCEPT ({gloss_vals})", profile, WAREHOUSE_ID)
    if st != "SUCCEEDED":
        print(f"FATAL: drift-guard direction A query failed (state={st}).", file=sys.stderr)
        sys.exit(4)
    orphan_array = [r[0] for r in rows_a] if rows_a else []

    # Direction B: glossary system terms never used as an enrichment array value.
    st, rows_b = run_sql(f"({gloss_vals}) EXCEPT ({array_vals})", profile, WAREHOUSE_ID)
    if st != "SUCCEEDED":
        print(f"FATAL: drift-guard direction B query failed (state={st}).", file=sys.stderr)
        sys.exit(4)
    orphan_gloss = [r[0] for r in rows_b] if rows_b else []

    print(f"  A) array-values EXCEPT category=system : {orphan_array or '∅ (empty)'}")
    print(f"  B) category=system EXCEPT array-values : {orphan_gloss or '∅ (empty)'}")

    if orphan_array:
        print(f"FATAL: enrichment emitted systems NOT in the approved glossary: {orphan_array}. "
              "The enum coupling drifted — re-run enrichment against the current glossary.",
              file=sys.stderr)
        sys.exit(6)
    # Direction B non-empty is a WARNING, not a failure: an approved system term may
    # legitimately not appear in any ticket. It is surfaced but does not break the build.
    if orphan_gloss:
        print(f"[enrich] NOTE: approved system term(s) unused by any ticket: {orphan_gloss} "
              "(informational — not a drift failure).")
    print("[enrich] DRIFT-GUARD PASSED (no enrichment value outside the approved glossary).")



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
    ap = argparse.ArgumentParser(description="FIS silver→gold enrichment (ENR-01/02, GLO-02).")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true", help="run acceptance assertions after enrich")
    ap.add_argument("--verify-only", action="store_true", help="skip enrich; only run assertions")
    ap.add_argument("--drift-guard", action="store_true",
                    help="run the GLO-02 EXCEPT-both-ways enum⟷glossary coupling guard")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    host = assert_target_host(args.profile)  # Step 0 — T-4.1-01 host gate
    print(f"[enrich] Host gate OK: {host}")

    if args.drift_guard:
        drift_guard(args.profile)
        return

    if not args.verify_only:
        enrich(args.profile)
    if args.verify or args.verify_only:
        verify(args.profile)


if __name__ == "__main__":
    main()
