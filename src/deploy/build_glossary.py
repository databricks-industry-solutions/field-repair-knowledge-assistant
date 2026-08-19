#!/usr/bin/env python3
"""
Knowledge Agent — Phase 4.1 Plan 02, Task 1: two-source glossary_proposals.

Builds a two-source glossary merge in this repo's CLI-driven, host-gated harness (mirrors
data_generation/build_silver.py / preflight.py). No Spark session — every stage is a
`CREATE OR REPLACE TABLE ...` issued through `run_sql` against the --warehouse-id
resolved by env.py.

Two corpora, merged (docs win on definition/category):
  1. ServiceNow (bronze rnd_tickets): `ai_query` extracts candidate domain terms per
     ticket, materialized once (Pitfall 5), grounding-guarded to verbatim mentions;
     then a second `ai_query` proposes definition/category/confidence per candidate.
  2. Product docs (data/product_docs/*.md) + the 20 curated src/deploy/glossary.md
     terms — authoritative definitions/categories/aliases, pre-loaded as
     authoritative proposals (Product-docs decision option (b): the SME confirms
     rather than re-derives). Docs win on definition (confidence 0.9,
     authoritative=true, review_priority='confirm').

Stages (each a materialized table on serverless — never reference an ai_query
column twice):
  _glossary_sn_extract    — per-ticket ai_query term extraction
  _glossary_sn_candidates — explode + grounding guard + per-term aggregation (SQL)
  _glossary_sn_stage      — per-candidate ai_query definition proposal
  _glossary_docs          — authoritative doc/seed terms (built in Python → VALUES)
  glossary_proposals      — FULL OUTER JOIN merge (docs win)
  glossary                — governed table (empty until promote_glossary.py)

The exact glossary_proposals column names match the 04.1-02 interfaces block so
the SME app (04.1-03) ports with no schema change.

Usage:
    python3 enrich/build_glossary.py --profile serverless-stable
    python3 enrich/build_glossary.py --profile serverless-stable --verify
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

# --- Reuse the Phase 1 host-safety gate + SQL runner ------------------------
# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally. preflight.py
# and env.py live in THIS dir (the old REPO/"preflight" path no longer exists).
_HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO = _HERE.parent
sys.path.insert(0, str(_HERE))
from preflight import assert_target_host, run_sql  # noqa: E402
import preflight as _pf  # noqa: E402  (workspace_client for the SDK-based poller)

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical default values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
# Glossary mining reads only the raw ticket text (title/description/notes/
# close_notes), all of which live on the bronze `rnd_tickets` table. It reads
# bronze — NOT `rd_tasks_silver` — so the glossary can be built and SME-approved
# BEFORE the enrich pipeline runs (the pipeline now owns silver → serving, and its
# gold_enrichment step depends on the approved glossary; mining from silver would
# make that circular).
T_SOURCE = f"{FQ}.rnd_tickets"
T_GLOSSARY_PROP = f"{FQ}.glossary_proposals"
T_GLOSSARY = f"{FQ}.glossary"
DEFAULT_PROFILE = "serverless-stable"

# ai_query BATCH-capable endpoint. sonnet-5 is NOT batch-supported (Pitfall 2);
# sonnet-4-5 verified batch-capable on the reference workspace.
CHAT_ENDPOINT = "databricks-claude-sonnet-4-5"

DOCS_DIR = REPO / "data" / "product_docs"
GLOSSARY_MD = REPO / "agents" / "glossary.md"

# --- responseFormat schemas + system prompts (ported verbatim from 30_glossary.py) ---

EXTRACT_RF = json.dumps({"type": "json_schema", "json_schema": {"name": "terms", "strict": True, "schema": {
    "type": "object", "properties": {
        "terms": {"type": "array", "items": {"type": "object", "properties": {
            "term": {"type": "string"},
            "kind": {"type": "string", "enum": ["acronym", "product", "vendor", "system", "hardware", "software", "other"]}},
            "required": ["term", "kind"]}}},
    "required": ["terms"]}}}).replace("'", "\\'")

EXTRACT_SYS = ("Extract domain-specific terms from this roadside truck-screening R&D "
    "ticket: product names, vendor names, named hardware components, and named software/system "
    "or subsystem names (including their acronyms). Include multi-word terms (e.g. 'Controller "
    "Web Application', 'AUR Illuminator'). "
    "A token is NOT a term just because it is capitalized or an initialism. EXCLUDE generic "
    "computing/networking/electronics terms that are not specific to this domain, even when "
    "written in caps or as an initialism - e.g. IP, RAM, LED, USB, NIC, DHCP, NTP, DOT, GVW, "
    "WINDOWS, CPU, URL, GB, MB. EXCLUDE bare generic nouns (CAMERA, SERVICE, MODEM, FIBER, "
    "CABLE, POWER) - only include them as part of a specific named term (e.g. 'AUR Camera', "
    "'ATIS Service'). Also exclude location codes, person names, dates, ticket IDs, and log/code "
    "identifiers (e.g. thread names, class names). "
    "Only include terms that appear VERBATIM in the text - do not invent or expand. "
    "When unsure whether something is domain-specific, leave it out.").replace("'", "\\'")

PROP_RF = json.dumps({"type": "json_schema", "json_schema": {"name": "gloss", "strict": True, "schema": {
    "type": "object", "properties": {
        "is_domain_term": {"type": "boolean"},
        "definition": {"type": "string"},
        "category": {"type": "string", "enum": ["system", "hardware", "vendor", "software", "process", "other"]},
        "disambiguation": {"type": "string"},
        "confidence": {"type": "number"}},
    "required": ["is_domain_term", "definition", "category", "confidence"]}}}).replace("'", "\\'")

PROP_SYS = ("You build a domain glossary for roadside truck-screening. Given a candidate "
    "term mined from ServiceNow R&D tickets with evidence snippets AND task titles, decide if it is "
    "domain jargon, define it grounded in evidence, categorize it, and note disambiguation. USE THE "
    "TASK TITLES as cross-document context (an acronym in tasks about the HTS Controller Web Application "
    "likely relates to that controller). Be conservative on confidence.").replace("'", "\\'")


# --- Stage 1: ServiceNow ai_query extraction (materialize once) -------------

def sql_sn_extract():
    return f"""
CREATE OR REPLACE TABLE {FQ}._glossary_sn_extract AS
WITH t AS (SELECT number, title, description, notes, close_notes FROM {T_SOURCE})
SELECT number, coalesce(title,'') AS title,
  concat_ws(chr(10), coalesce(title,''), coalesce(description,''), coalesce(notes,''), coalesce(close_notes,'')) AS src_text,
  from_json(ai_query('{CHAT_ENDPOINT}',
    concat('{EXTRACT_SYS}',
      '\\n\\nTITLE: ', coalesce(title,''),
      '\\nDESCRIPTION:\\n', coalesce(description,''),
      '\\nNOTES:\\n', coalesce(notes,''),
      '\\nCLOSE:\\n', coalesce(close_notes,'')),
    responseFormat => '{EXTRACT_RF}'),
    'STRUCT<terms:ARRAY<STRUCT<term:STRING,kind:STRING>>>') AS ext
FROM t
""".strip()


# --- Stage 1b: explode + grounding guard + per-term aggregation (pure SQL) ---
# Replaces the reference's Python collect()/defaultdict reconstruction so no
# Spark session is needed. Grounding guard: term must appear verbatim
# (case-insensitive) in its ticket. Evidence = up to 3 "[num — title] …snippet…".

def sql_sn_candidates():
    return f"""
CREATE OR REPLACE TABLE {FQ}._glossary_sn_candidates AS
WITH exploded AS (
  SELECT number, title, src_text, trim(e.term) AS term, e.kind AS kind
  FROM {FQ}._glossary_sn_extract LATERAL VIEW explode(ext.terms) x AS e
),
grounded AS (
  SELECT number, title, src_text, term, kind
  FROM exploded
  WHERE term IS NOT NULL AND length(term) > 1 AND contains(lower(src_text), lower(term))
)
SELECT
  max(term)                                        AS term,
  max(kind)                                        AS kind,
  count(DISTINCT number)                           AS num_tasks,
  slice(array_sort(collect_set(number)), 1, 6)     AS sample_tasks,
  array_join(
    slice(
      collect_list(
        concat('[', number, ' — "', coalesce(title,''), '"] …',
          substr(src_text, greatest(1, locate(lower(term), lower(src_text)) - 45), 120), '…')),
      1, 3),
    ' | ')                                         AS evidence
FROM grounded
GROUP BY upper(term)
""".strip()


# --- Stage 2: FM proposes definition/category/confidence (materialize once) --

def sql_sn_stage():
    return f"""
CREATE OR REPLACE TABLE {FQ}._glossary_sn_stage AS
SELECT term, kind, num_tasks, sample_tasks,
  from_json(ai_query('{CHAT_ENDPOINT}',
    concat('{PROP_SYS}','\\n\\nTERM: ',term,' (in ',num_tasks,' tasks)\\nEVIDENCE: ',coalesce(evidence,'')),
    responseFormat => '{PROP_RF}'),
    'STRUCT<is_domain_term:BOOLEAN,definition:STRING,category:STRING,disambiguation:STRING,confidence:DOUBLE>') AS p
FROM {FQ}._glossary_sn_candidates
""".strip()


# --- Stage 3: authoritative doc + curated-seed terms (parsed in Python) ------

# Explicit categories/aliases for the 20 curated src/deploy/glossary.md terms. These
# are the authoritative seed (Product-docs decision option (b)): the SME confirms
# these rather than re-deriving them. AUR/OVC are HARDWARE here and get
# recategorized to `system` at promote time (promote_glossary.py) — the enrichment
# systems enum depends on that edit.
SEED_TERMS = [
    # term, expansion, category, aliases, definition
    ("CA", "Controller Application", "software", ["Controller Application", "HTS Controller Web Application"],
     "The control/operator software layer for a roadside HTS installation; a component of the HTS controller stack that fuses sensor data into vehicle events and serves the web UI."),
    ("HTS", "Highway Truck Screening", "system", ["HTS system"],
     "Highway/roadside controller platform that hosts the Controller Application (CA) and coordinates attached sensors, cameras, and the web app."),
    ("LoopSense", None, "system", [],
     "Inductive-loop vehicle-detection subsystem used for presence and counting at a lane/site."),
    ("SRA", "Sensor Relay Assembly", "hardware", ["Sensor Relay Assembly"],
     "Powered relay/interface unit that bridges in-road sensors (WIM, VWI) to the controller; restarting the SRA power-cycles attached sensor bars."),
    ("WIM", "Weigh-In-Motion", "system", ["Weigh-In-Motion", "Weigh In Motion"],
     "System that measures axle/vehicle weights while a vehicle is moving over in-road sensors. Failures commonly present as zero or implausible weight readings."),
    ("Veridyne", None, "vendor", [],
     "Sensor vendor whose quartz/piezo strip sensors are used in WIM installations."),
    ("axle sensor", None, "hardware", [],
     "In-road sensor that detects/weighs individual axles; an input to the WIM computation."),
    ("AUR", "Automatic USDOT Reader", "hardware", ["Automatic USDOT Reader", "AUR camera"],
     "Camera unit that reads USDOT numbers and hazard placards from the tractor; needs illuminators at night. Referenced in imaging/ALPR contexts."),
    ("OVC", "OVerview Camera", "hardware", ["OVerview Camera", "Overview Camera"],
     "Wide-angle context camera unit used in roadside imaging; can also serve as a trigger source (the OVC loop)."),
    ("ALPR", "Automatic License Plate Recognition", "system", ["Automatic License Plate Recognition"],
     "Capability (and the cameras serving it) that reads plate characters from vehicle images and determines issuing jurisdiction."),
    ("Aptix", None, "vendor", ["Lumex"],
     "Vendors associated with ALPR cameras and readers. Legacy Aptix cameras are being phased out in favour of Lumex."),
    ("CamView", None, "software", ["CamView Viewer"],
     "Camera viewer / SDK tooling used to view or configure machine-vision camera streams."),
    ("illuminator", None, "hardware", ["AUR illuminator"],
     "IR/visible lighting unit paired with a camera to enable capture in low light."),
    ("WPS", "PowerNode", "hardware", ["PowerNode"],
     "Networked power controller (a PowerNode unit) used to remotely power-cycle roadside equipment. WPS and PowerNode refer to the same power-controller role."),
    ("ATIS", "Axle & Tire Imaging System", "system", ["Axle & Tire Imaging System", "Advanced Traveler Information System"],
     "Enclosure-mounted cameras that image each axle/tire for axle-count and tire condition."),
]

# Product-docs subsystem-glossary table rows (01_platform_overview.md) are mined
# with the reference TABLE_ROW_RE. Category for these doc-table terms is inferred
# from a small keyword map (system/hardware/software/vendor) so downstream
# category-driven filtering is deterministic; anything unmapped stays NULL and the
# ServiceNow FM proposal fills it in at merge time.
TABLE_ROW_RE = re.compile(r"^\|\s*\*{0,2}([A-Z][A-Za-z0-9 /&]{0,30}?)\*{0,2}\s*\|\s*\*{0,2}(.*?)\*{0,2}\s*\|\s*(.*?)\s*\|\s*$")
DOC_CATEGORY_HINTS = {
    "ATPS": "system", "SRIS": "system", "VWI": "hardware", "OTA": "hardware",
    "DAP": "hardware", "VES": "software", "DW": "vendor", "VLS": "software",
}


def strip_md(s):
    return re.sub(r"\*+", "", s or "").strip()


def parse_authoritative_terms():
    """Build the authoritative doc/seed term list (dedup by upper(term))."""
    by_key = {}

    # 1) curated seed (src/deploy/glossary.md), authoritative + explicit category/aliases.
    for term, expansion, category, aliases, definition in SEED_TERMS:
        by_key[term.upper()] = {
            "term": term, "expansion": expansion, "definition": definition,
            "category": category, "aliases": list(aliases), "source_doc": "src/deploy/glossary.md",
        }

    # 2) product-docs subsystem-glossary tables (adds ATPS/SRIS/VWI/OTA/DAP/VES/...).
    for md in sorted(DOCS_DIR.glob("*.md")):
        if md.name.startswith("00_"):
            continue
        for line in md.read_text().splitlines():
            m = TABLE_ROW_RE.match(line)
            if not m:
                continue
            term = strip_md(m.group(1))
            exp = strip_md(m.group(2))
            desc = strip_md(m.group(3))
            if term.lower() in {"acronym", "term", "expansion"} or set(term) <= set("-| ") or not term or term == "—":
                continue
            key = term.upper().split(" / ")[0]
            base_term = term.split(" / ")[0].strip()
            if key in by_key or key in {"POE", "GVW"}:
                continue  # curated seed wins; skip generic tokens
            expansion = None if exp in ("—", "") else exp
            aliases = []
            # "VLS / Vehicle Live Summary" style: the second name is an alias.
            if " / " in term:
                aliases.append(term.split(" / ", 1)[1].strip())
            if expansion:
                aliases.append(expansion)
            by_key[key] = {
                "term": base_term, "expansion": expansion,
                "definition": desc or exp, "category": DOC_CATEGORY_HINTS.get(key),
                "aliases": aliases, "source_doc": md.name,
            }
    return list(by_key.values())


def q(s):
    """SQL single-quoted literal (escape embedded quotes by doubling)."""
    if s is None:
        return "NULL"
    return "'" + str(s).replace("'", "''") + "'"


def arr(vals):
    """SQL array<string> literal, de-duplicated, empty → array()."""
    seen, out = set(), []
    for v in vals or []:
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    if not out:
        return "array()"
    return "array(" + ",".join(q(v) for v in out) + ")"


def sql_docs_table():
    rows = parse_authoritative_terms()
    values = []
    for r in rows:
        cat = q(r["category"]) if r["category"] else "cast(NULL AS STRING)"
        values.append(
            f"({q(r['term'])}, {q(r['expansion'])}, {q(r['definition'])}, {cat}, "
            f"{arr(r['aliases'])}, {q(r['source_doc'])})"
        )
    values_sql = ",\n  ".join(values)
    return f"""
CREATE OR REPLACE TABLE {FQ}._glossary_docs AS
SELECT * FROM VALUES
  {values_sql}
AS t(term, expansion, definition, category, aliases, source_doc)
""".strip(), len(rows)


# --- Stage 4: merge (docs win on definition + category) → glossary_proposals -

def sql_merge():
    return f"""
CREATE OR REPLACE TABLE {T_GLOSSARY_PROP} AS
WITH sn AS (SELECT upper(term) k, term, kind, num_tasks, sample_tasks, p.* FROM {FQ}._glossary_sn_stage),
     d  AS (SELECT upper(term) k, * FROM {FQ}._glossary_docs)
SELECT
  coalesce(d.term, sn.term)                                          AS term,
  coalesce(d.definition, sn.definition)                              AS definition,
  d.expansion                                                        AS expansion,
  coalesce(d.category, sn.category)                                  AS category,
  sn.num_tasks                                                       AS num_tasks,
  sn.sample_tasks                                                    AS source_refs,
  coalesce(sn.disambiguation,'')                                     AS disambiguation,
  CASE WHEN d.k IS NOT NULL THEN 0.9 ELSE coalesce(sn.confidence,0) END       AS confidence,
  CASE WHEN d.k IS NOT NULL THEN true ELSE coalesce(sn.is_domain_term,false) END AS is_domain_term,
  (d.k IS NOT NULL)                                                  AS authoritative,
  CASE WHEN d.k IS NOT NULL AND sn.k IS NOT NULL THEN 'product_docs+servicenow'
       WHEN d.k IS NOT NULL THEN 'product_docs'
       ELSE 'servicenow' END                                        AS source_corpus,
  CASE WHEN d.k IS NOT NULL THEN 'confirm'
       WHEN coalesce(sn.is_domain_term,false) AND coalesce(sn.confidence,0) < 0.7 THEN 'needs_review'
       WHEN coalesce(sn.is_domain_term,false) THEN 'confirm' ELSE 'likely_drop' END AS review_priority,
  sn.definition                                                      AS sn_guess,
  'proposed'                                                         AS status,
  cast(NULL AS STRING)    AS review_decision,
  cast(NULL AS STRING)    AS reviewed_by,
  cast(NULL AS TIMESTAMP) AS reviewed_at,
  cast(NULL AS STRING)    AS edited_definition,
  cast(NULL AS STRING)    AS edited_category,
  -- authoritative curated aliases seed edited_aliases so promote() carries them
  -- into the governed glossary (GLO-04 alias resolution) without re-typing.
  CASE WHEN d.k IS NOT NULL THEN d.aliases ELSE cast(NULL AS ARRAY<STRING>) END AS edited_aliases,
  current_timestamp()     AS proposed_at
FROM sn FULL OUTER JOIN d ON sn.k = d.k
""".strip()


def sql_governed_glossary():
    return f"""
CREATE TABLE IF NOT EXISTS {T_GLOSSARY} (
  term STRING, definition STRING, aliases ARRAY<STRING>, category STRING,
  source_refs ARRAY<STRING>, source_corpus STRING, status STRING,
  approved_by STRING, approved_at TIMESTAMP, version INT)
COMMENT 'Governed glossary — SME-approved terms only.'
""".strip()


def sql_glossary_function():
    """The `glossary_lookup(term_query)` UC function the Multi-Agent Supervisor routes
    to (Tool 3). Returns the SME-APPROVED definition + category for a term or one of
    its aliases. Nothing else in the repo created it, so a fresh deploy needs it here.
    """
    return f"""
CREATE OR REPLACE FUNCTION {T_GLOSSARY}_lookup(term_query STRING)
RETURNS TABLE (term STRING, definition STRING, category STRING)
COMMENT 'SME-approved definition + category for a glossary term or alias.'
RETURN
  SELECT term, definition, category
  FROM {T_GLOSSARY}
  WHERE status = 'approved'
    AND (
      lower(term) = lower(term_query)
      OR exists(aliases, a -> lower(a) = lower(term_query))
      OR lower(term) LIKE concat('%', lower(term_query), '%')
    )
""".strip()


def sql_seed_authoritative():
    """Promote the CURATED AUTHORITATIVE terms (product-docs/glossary.md seed) to
    status='approved' in the governed glossary.

    GLO-02 keeps ServiceNow-MINED proposals in glossary_proposals for SME review, but
    the curated seed (authoritative=true) is pre-vetted — so it is auto-approved here.
    Without at least one approved category='system' term (and one approved acronym),
    the enrichment pipeline refuses to run (by design). Idempotent: inserts only terms
    not already in the governed glossary, so a manual SME approval is never overwritten.
    """
    return f"""
INSERT INTO {T_GLOSSARY}
  (term, definition, aliases, category, source_refs, source_corpus, status,
   approved_by, approved_at, version)
SELECT p.term, p.definition,
       coalesce(p.edited_aliases, cast(array() AS ARRAY<STRING>)),
       p.category, p.source_refs, p.source_corpus,
       'approved', 'auto-seed:authoritative', current_timestamp(), 1
FROM {T_GLOSSARY_PROP} p
WHERE p.authoritative = true AND p.category IS NOT NULL
  AND upper(p.term) NOT IN (SELECT upper(term) FROM {T_GLOSSARY})
""".strip()


# --- runners ----------------------------------------------------------------

def run_sql_poll(statement, profile, warehouse_id, max_wait_s=1200, poll_s=10):
    """Submit a statement and POLL statement_id to completion.

    preflight.run_sql uses a 50s server-side wait_timeout and returns PENDING for
    long ai_query batch stages (Pitfall 5 scale note: ~200 model calls). Here we
    submit, and if not terminal, poll GET /statements/{id} until SUCCEEDED/FAILED.
    Returns (state, data_array).
    """
    from databricks.sdk.service.sql import StatementState  # noqa: E402
    w = _pf.workspace_client(profile)  # ambient in-job, CLI profile locally
    try:
        resp = w.statement_execution.execute_statement(
            warehouse_id=warehouse_id, statement=statement, wait_timeout="30s")
        sid = resp.statement_id
        deadline = time.time() + max_wait_s
        while (sid and resp.status and resp.status.state in
               (StatementState.PENDING, StatementState.RUNNING)
               and time.time() < deadline):
            time.sleep(poll_s)
            resp = w.statement_execution.get_statement(sid)
    except Exception as e:  # noqa: BLE001
        print(f"  statement submit/poll error: {e}", file=sys.stderr)
        return "CLI_ERROR", None
    state = resp.status.state.value if resp.status and resp.status.state else "UNKNOWN"
    if state == "FAILED" and resp.status and resp.status.error:
        print(f"  statement error: {(resp.status.error.message or '')[:300]}",
              file=sys.stderr)
    return state, (resp.result.data_array if resp.result else None)


def run_or_die(label, stmt, profile):
    state, data = run_sql_poll(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} failed (state={state}).", file=sys.stderr)
        print(stmt[:2000], file=sys.stderr)
        sys.exit(4)
    print(f"[glossary] {label}: SUCCEEDED")
    return data


def scalar(stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return data[0][0], state


def build(profile):
    print("[glossary] Stage 1: ServiceNow ai_query term extraction (materialize)...")
    run_or_die("_glossary_sn_extract", sql_sn_extract(), profile)
    print("[glossary] Stage 1b: explode + grounding guard + aggregate...")
    run_or_die("_glossary_sn_candidates", sql_sn_candidates(), profile)
    print("[glossary] Stage 2: FM propose definition/category/confidence (materialize)...")
    run_or_die("_glossary_sn_stage", sql_sn_stage(), profile)
    print("[glossary] Stage 3: authoritative doc + curated-seed terms...")
    docs_stmt, n_docs = sql_docs_table()
    run_or_die(f"_glossary_docs ({n_docs} authoritative terms)", docs_stmt, profile)
    print("[glossary] Stage 4: merge (docs win) → glossary_proposals...")
    run_or_die("glossary_proposals", sql_merge(), profile)
    run_or_die("governed glossary (table)", sql_governed_glossary(), profile)
    print("[glossary] Stage 5: seed authoritative curated terms as APPROVED...")
    run_or_die("seed authoritative approved terms", sql_seed_authoritative(), profile)
    print("[glossary] Stage 6: glossary_lookup UC function (MAS Tool 3)...")
    run_or_die("glossary_lookup UC function", sql_glossary_function(), profile)


def verify(profile):
    print("[glossary] --verify: running acceptance assertions...")
    checks = []

    n, _ = scalar(f"SELECT count(*) FROM {T_GLOSSARY_PROP}", profile)
    checks.append(("glossary_proposals count > 20", n is not None and int(n) > 20, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GLOSSARY_PROP} WHERE source_corpus LIKE '%product_docs%'", profile)
    checks.append(("product_docs-sourced rows > 0", n is not None and int(n) > 0, f"{n}"))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GLOSSARY_PROP} WHERE source_corpus = 'servicenow'", profile)
    checks.append(("servicenow-only rows > 0 (both sources)", n is not None and int(n) > 0, f"{n}"))

    state, rows = run_sql(
        f"SELECT review_priority, count(*) FROM {T_GLOSSARY_PROP} GROUP BY review_priority", profile, WAREHOUSE_ID)
    buckets = {r[0] for r in rows} if rows else set()
    checks.append(("confirm + >=1 other review_priority bucket",
                   "confirm" in buckets and len(buckets - {"confirm"}) >= 1, str(sorted(buckets))))

    n, _ = scalar(
        f"SELECT count(*) FROM {T_GLOSSARY_PROP} WHERE upper(term)='CA' AND authoritative=true", profile)
    checks.append(("CA present authoritative=true (glossary.md seed)", n is not None and int(n) >= 1, f"{n}"))

    cat, _ = scalar(f"SELECT category FROM {T_GLOSSARY_PROP} WHERE upper(term)='CA'", profile)
    checks.append(("CA category == software", cat == "software", f"{cat}"))

    # governed glossary table exists (row count may be 0 pre-promote)
    st, _ = scalar(f"SELECT count(*) FROM {T_GLOSSARY}", profile)
    checks.append(("governed glossary table exists", st is not None, f"rows={st}"))

    print("\n[glossary] Acceptance results:")
    all_ok = True
    for label, ok, ev in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  (observed: {ev})")
        all_ok = all_ok and ok
    if not all_ok:
        print("[glossary] VERIFY FAILED", file=sys.stderr)
        sys.exit(5)
    print("[glossary] VERIFY PASSED")



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
    ap = argparse.ArgumentParser(description="Build the two-source glossary_proposals (GLO-01).")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true", help="run acceptance assertions after build")
    ap.add_argument("--verify-only", action="store_true", help="skip build; only run assertions")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    host = assert_target_host(args.profile)  # Step 0 — T-4.1-01 host gate
    print(f"[glossary] Host gate OK: {host}")

    if not args.verify_only:
        build(args.profile)
    if args.verify or args.verify_only:
        verify(args.profile)


if __name__ == "__main__":
    main()
