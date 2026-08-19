#!/usr/bin/env python3
"""
Knowledge Agent — Phase 3 ai_query narrative generation.

Runs ONE batch `ai_query` pass (databricks-claude-haiku-4-5, json_schema
responseFormat, failOnError => false) over the `syn_seeds` staging table and
materializes the raw free-text narratives ONCE into `syn_generated_raw`. The
LLM fabricates ONLY the prose fields (title / description / notes[] /
close_notes); every schema-bearing value stays deterministic in the seed and is
typed later by `synth/postprocess.py` (D-01).

Design (per CONTEXT D-01/D-03/D-08 + RESEARCH §Generation Architecture):
  - VERIFIED call shape (live round-trip 2026-07-22): responseFormat MUST be
    the json_schema form — the plain object-type response format FAILS on haiku
    with INVALID_PARAMETER_VALUE. temperature 0.9 for length/tone variety.
  - failOnError => false so one bad generation cannot abort the batch
    (T-03-06); gen is STRUCT{result, errorMessage} (live-verified field names —
    the payload is `gen.result`, NOT `gen.response`). After the write, count
    rows where gen.errorMessage IS NOT NULL and re-run generation for JUST those
    seed_ids (delete-then-insert of the failed ids) until zero.
  - The prompt is assembled IN SQL from `syn_seeds` columns ONLY (equipment,
    failure_mode, location, outcome_tag, note_author_plan, acronym_flag,
    priority_label, status). The answer-key file is NEVER referenced (D-03) —
    this file does not open it and embeds no answer-key phrasing.
  - Few-shot the model with 2-3 real dated-note exemplars so tone/format match
    the fingerprint; instruct WIDE length variety (reject uniform) and — for
    acronym-flagged seeds — inline expansion ("the Controller Application (CA)
    crashed", "swapped the PowerNode (WPS power controller)"). The
    post-processor re-dates/re-authors note lines deterministically, so the
    model is told to write plain note sentences (no dates/initials needed).
  - Materialize-once: CREATE OR REPLACE TABLE ... AS SELECT ai_query(...). The
    staging table is private (not the real corpus) — REPLACE is safe.

Usage:
    python3 synth/generate.py --profile serverless-stable
    python3 synth/generate.py --profile serverless-stable --max-retries 3
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Repo-root sys.path shim (mirrors build_taxonomy.py) so preflight resolves.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from preflight.preflight import (  # noqa: E402
    DEMO_CATALOG,
    DEMO_SCHEMA,
    DEFAULT_PROFILE,
    assert_target_host,
    first_warehouse_id,
    run_sql,
)


def run_sql_poll(statement, profile, warehouse_id, poll_timeout=1200):
    """Run a long SQL statement, polling the statements API until it finishes.

    A 200-row `ai_query` batch far exceeds the synchronous 50s wait window, so
    `preflight.run_sql` returns PENDING before it completes. This submits with
    wait_timeout=0s (async), then GETs the statement id until it reaches a
    terminal state. Returns (state, data_array).
    """
    payload = json.dumps({
        "warehouse_id": warehouse_id,
        "statement": statement,
        "wait_timeout": "0s",
    })
    p = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", payload, "--profile", profile],
        capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        return "CLI_ERROR", None
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        return "PARSE_ERROR", None
    statement_id = d.get("statement_id")
    state = d.get("status", {}).get("state", "UNKNOWN")
    deadline = time.time() + poll_timeout
    while state in ("PENDING", "RUNNING") and time.time() < deadline:
        time.sleep(5)
        g = subprocess.run(
            ["databricks", "api", "get",
             f"/api/2.0/sql/statements/{statement_id}", "--profile", profile],
            capture_output=True, text=True, timeout=180)
        if g.returncode != 0:
            return "CLI_ERROR", None
        try:
            d = json.loads(g.stdout)
        except json.JSONDecodeError:
            return "PARSE_ERROR", None
        state = d.get("status", {}).get("state", "UNKNOWN")
    return state, d.get("result", {}).get("data_array")

FQ = f"{DEMO_CATALOG}.{DEMO_SCHEMA}"
SEEDS = f"{FQ}.syn_seeds"
RAW = f"{FQ}.syn_generated_raw"

GEN_MODEL = "databricks-claude-haiku-4-5"

# VERIFIED working responseFormat (live 2026-07-22): the json_schema form, NOT
# the plain object-type response format (which FAILS on haiku with
# INVALID_PARAMETER_VALUE). Single-line JSON so it embeds cleanly as a SQL
# string literal.
RESPONSE_FORMAT = (
    '{"type":"json_schema","json_schema":{"name":"ticket",'
    '"schema":{"type":"object","properties":{'
    '"title":{"type":"string"},"description":{"type":"string"},'
    '"notes":{"type":"array","items":{"type":"string"}},'
    '"close_notes":{"type":"string"}},'
    '"required":["title","description","notes","close_notes"]},"strict":true}}'
)

# 2-3 real-style dated-note exemplars (tone/format few-shot). These are STYLE
# samples of the note fingerprint — recombined generic phrasing, NOT answer-key
# text. The post-processor owns the actual dates/authors, so the model only
# needs to match sentence tone here.
FEWSHOT = (
    "Example note style (match this terse, field-log tone; do NOT copy the "
    "text): "
    "\"currently have cases open, waiting on parts to arrive before revisiting\"; "
    "\"went onsite, power-cycled the enclosure, camera came back briefly then "
    "dropped again\"; "
    "\"reverted the config change, still seeing intermittent drops, escalating\"."
)


def sql_str(value):
    """SQL string literal with single-quotes doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def build_prompt_expr():
    """A SQL expression assembling the per-row generation prompt from syn_seeds.

    Uses ONLY seed columns (D-03). Emits an acronym instruction only when
    acronym_flag <> 'none' (D-08). All literal fragments are recombined domain
    guidance — no answer-key phrasing.
    """
    base_instructions = (
        "You are writing a realistic internal ServiceNow R&D troubleshooting "
        "ticket for a roadside sensor/camera support team. Fabricate ONLY "
        "free-text narrative. Do NOT invent ticket numbers, dates, people "
        "names, priorities, or statuses. "
        "Return a JSON object with keys: title (short summary line), "
        "description (1-3 sentences stating the problem as first reported), "
        "notes (an ARRAY of 2-6 short plain troubleshooting log sentences, "
        "written as a field engineer's running notes; do NOT prefix them with "
        "dates or initials), and close_notes (a resolution/closure sentence if "
        "the case is closed, else a brief current-status sentence). "
        "VARY the length a LOT across tickets: some terse (a single short "
        "note), some detailed (several notes) — never uniform. Use the real "
        "equipment/site vocabulary given below verbatim. " + FEWSHOT
    )
    acronym_expr = (
        "CASE WHEN s.acronym_flag = 'CA' THEN "
        "' Naturally expand the acronym inline the first time, e.g. write "
        "\"the Controller Application (CA)\" so the term and its expansion "
        "co-occur.' "
        "WHEN s.acronym_flag = 'WPS' THEN "
        "' Naturally expand the acronym inline the first time, e.g. write "
        "\"the PowerNode (WPS power controller)\" so the term and its "
        "expansion co-occur.' "
        "ELSE '' END"
    )
    outcome_expr = (
        "CASE "
        "WHEN s.outcome_tag = 'resolved-clean' THEN "
        "' This case was resolved cleanly; close_notes should state the fix.' "
        "WHEN s.outcome_tag = 'failed-repeatedly' THEN "
        "' This case was revisited several times and still is not fully fixed; "
        "the notes should reflect repeated attempts and lingering issues.' "
        "WHEN s.outcome_tag = 'near-miss' THEN "
        "' This case was closed but the fix was only partial or the root cause "
        "differed from the first guess.' "
        "ELSE "
        "' This case is still open/pending with no final resolution yet.' "
        "END"
    )
    return (
        f"concat("
        f"{sql_str(base_instructions)}, "
        f"'\\n\\nEquipment: ', s.equipment, "
        f"'\\nReported problem: ', s.failure_mode, "
        f"'\\nSite/location: ', s.location, "
        f"'\\nPriority: ', s.priority_label, "
        f"'\\nStatus: ', s.status, "
        f"{outcome_expr}, "
        f"{acronym_expr}"
        f")"
    )


def create_raw_sql(where_seed_ids=None):
    """Build the generation SQL.

    If `where_seed_ids` is None: CREATE OR REPLACE the whole table (initial
    materialize-once pass). Otherwise: SELECT only the given seed_ids (used by
    the retry path, which deletes-then-inserts the failed ids).
    """
    prompt_expr = build_prompt_expr()
    select = (
        f"SELECT s.seed_id, "
        f"ai_query("
        f"'{GEN_MODEL}', "
        f"{prompt_expr}, "
        f"responseFormat => {sql_str(RESPONSE_FORMAT)}, "
        f"modelParameters => named_struct('temperature', CAST(0.9 AS DOUBLE), "
        f"'max_tokens', 1200), "
        f"failOnError => false"
        f") AS gen "
        f"FROM {SEEDS} s"
    )
    if where_seed_ids is None:
        return f"CREATE OR REPLACE TABLE {RAW} AS\n{select}"
    ids = ", ".join(str(int(i)) for i in where_seed_ids)
    return f"{select} WHERE s.seed_id IN ({ids})"


def exec_sql(stmt, profile, warehouse_id, label):
    state, data = run_sql_poll(stmt, profile, warehouse_id)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} did not succeed (state={state}).", file=sys.stderr)
        print(f"  statement head: {stmt[:200]}", file=sys.stderr)
        sys.exit(4)
    return data


def count_status(profile, warehouse_id):
    """Return (total, ok, errored) counts over syn_generated_raw."""
    _, data = run_sql(
        f"SELECT COUNT(*), "
        f"SUM(CASE WHEN gen.errorMessage IS NULL THEN 1 ELSE 0 END), "
        f"SUM(CASE WHEN gen.errorMessage IS NOT NULL THEN 1 ELSE 0 END) "
        f"FROM {RAW}", profile, warehouse_id)
    if not data:
        return 0, 0, 0
    row = data[0]
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))


def errored_seed_ids(profile, warehouse_id):
    _, data = run_sql(
        f"SELECT seed_id FROM {RAW} WHERE gen.errorMessage IS NOT NULL "
        f"ORDER BY seed_id", profile, warehouse_id)
    return [int(r[0]) for r in (data or [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--max-retries", type=int, default=3,
                    help="max regeneration passes for errored seed_ids")
    args = ap.parse_args()

    # Step 0 — never write to the wrong/unauthenticated workspace (T-03-02).
    host = assert_target_host(args.profile)
    warehouse_id = first_warehouse_id(args.profile)
    if not warehouse_id:
        print("FATAL: no serverless warehouse resolved; cannot generate.",
              file=sys.stderr)
        sys.exit(2)

    _, seed_cnt = run_sql(f"SELECT COUNT(*) FROM {SEEDS}", args.profile,
                          warehouse_id)
    n_seeds = int(seed_cnt[0][0]) if seed_cnt else 0
    print(f"Generating narratives for {n_seeds} seeds via {GEN_MODEL} "
          f"(json_schema, temp 0.9, failOnError false) on {host}")

    # 1. Materialize-once: one ai_query batch pass over all seeds.
    exec_sql(create_raw_sql(), args.profile, warehouse_id,
             "CREATE OR REPLACE syn_generated_raw")

    total, ok, errored = count_status(args.profile, warehouse_id)
    print(f"  initial pass: total={total}, ok={ok}, errored={errored}")

    # 2. Retry ONLY the errored seed_ids (delete-then-insert) until zero.
    attempt = 0
    while errored > 0 and attempt < args.max_retries:
        attempt += 1
        ids = errored_seed_ids(args.profile, warehouse_id)
        print(f"  retry {attempt}/{args.max_retries}: regenerating "
              f"{len(ids)} errored seed_id(s)")
        id_list = ", ".join(str(i) for i in ids)
        exec_sql(f"DELETE FROM {RAW} WHERE seed_id IN ({id_list})",
                 args.profile, warehouse_id, f"DELETE errored ids (retry {attempt})")
        exec_sql(f"INSERT INTO {RAW}\n{create_raw_sql(where_seed_ids=ids)}",
                 args.profile, warehouse_id, f"INSERT regenerated (retry {attempt})")
        total, ok, errored = count_status(args.profile, warehouse_id)
        print(f"    after retry {attempt}: total={total}, ok={ok}, errored={errored}")

    print(f"Generation complete: syn_generated_raw total={total}, "
          f"ok={ok}, errored={errored}")
    if errored > 0:
        print(f"WARNING: {errored} generation(s) still errored after "
              f"{args.max_retries} retries — logged above; postprocess will "
              f"route them to its skipped list (never crashes).",
              file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
