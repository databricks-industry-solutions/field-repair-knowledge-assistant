#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 4 Plan 01: build the Knowledge Assistant.

Stands up a live Agent Bricks Knowledge Assistant over the 223-ticket R&D corpus:

  1. Create a MANAGED UC Volume for a curated acronym glossary, upload glossary.md.
  2. Create the KA via /api/2.1/knowledge-assistants (NOT /api/2.0/tiles) with
     anti-leakage, hedge-and-cite instructions (D-04).
  3. Attach TWO knowledge sources:
       - source 1: the rnd_tickets Delta table (content col case_text)   [D-01]
       - source 2: the glossary.md file in the new Volume                 [D-03]
  4. Trigger :sync and POLL to ACTIVE / UPDATED (no fixed timer — Pitfall 6).
  5. Fire one live terminology query at the serving endpoint and dump the RAW
     response so the citation-payload shape can be inspected (A2 spike).
  6. Record KA id, serving endpoint_name, indexing wall-clock, confirmed
     source_type wire string, and the citation shape into 04-KA-BUILD.md.

Design notes / spikes resolved this build:
  * source_type wire string (A1 spike): the databricks-sdk>=0.122.0
    KnowledgeSource docstring pins the valid values as the LOWERCASE strings
    "file_table", "files", "index" — NOT the uppercase FILE_TABLE/FILES the
    research ASSUMED. This script sends the confirmed lowercase values.
  * REST paths (verified from SDK 0.122.0 .do() calls):
       POST /api/2.1/knowledge-assistants
       POST /api/2.1/{name}/knowledge-sources          (name = knowledge-assistants/{id})
       POST /api/2.1/{name}/knowledge-sources:sync
       GET  /api/2.1/{name}
  * The installed global databricks-sdk is 0.96.0 (lacks the KA API), so this
    script drives the REST paths directly via the healthy `databricks` CLI OAuth
    (--profile serverless-stable) — no SDK dependency at runtime.

Reuses the host-assertion gate + run_cli/run_sql pattern from preflight/preflight.py
so the build refuses to run against any workspace but fevm-serverless-stable-l26d62.

Usage:
    python3 agents/build_ka.py --profile serverless-stable
    python3 agents/build_ka.py --profile serverless-stable --skip-query   # build only
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Reused constants (mirror preflight/preflight.py) -----------------------
TARGET_HOST_FRAGMENT = "fevm-serverless-stable-l26d62"

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
# NOTE: this module reimplements the preflight helpers rather than importing them,
# so preflight/ is NOT already on sys.path here — it must be added.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preflight"))
import env as _env  # noqa: E402

DEMO_CATALOG = _env.CATALOG
DEMO_SCHEMA = _env.SCHEMA
DEFAULT_PROFILE = "serverless-stable"
WAREHOUSE_ID = _env.WAREHOUSE_ID

# KA build targets
KA_DISPLAY_NAME = "fis-rnd-knowledge-assistant"
GLOSSARY_VOLUME = "glossary"
CORPUS_TABLE = f"{DEMO_CATALOG}.{DEMO_SCHEMA}.rnd_tickets"
CORPUS_CONTENT_COL = "case_text"
# ENR-03 repoint target: the segmented, glossary-acronym-expanded content column
# (built by enrich/build_ka_content.py on the same rnd_tickets table, so the KA
# citation metadata struct — which carries the ticket number — is preserved).
CORPUS_ENRICHED_COL = "ka_content"
CORPUS_SOURCE_NAME = "rnd_tickets_corpus"
GLOSSARY_VOLUME_PATH = f"/Volumes/{DEMO_CATALOG}/{DEMO_SCHEMA}/{GLOSSARY_VOLUME}"
GLOSSARY_FILE_PATH = f"{GLOSSARY_VOLUME_PATH}/glossary.md"

# CONFIRMED source_type wire strings (A1 spike — from SDK 0.122.0 docstring).
SOURCE_TYPE_FILE_TABLE = "file_table"
SOURCE_TYPE_FILES = "files"

# Poll config (no fixed completion timer — Pitfall 6; just a safety ceiling).
POLL_INTERVAL_S = 30
POLL_CEILING_S = 60 * 90  # 90 min safety ceiling; report if exceeded, don't hang forever

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCAL_GLOSSARY = Path(__file__).resolve().parent / "glossary.md"
BUILD_DOC = (
    REPO_ROOT
    / ".planning/phases/04-knowledge-assistant-genie-space/04-KA-BUILD.md"
)

# KA instructions — hedge + cite + anti-leakage (D-04).
#
# THESE ARE THE LIVE, TUNED INSTRUCTIONS, recovered from the running
# `fis-rnd-knowledge-assistant` on 2026-08-05. The prose version that used to
# live here had DRIFTED from what was actually deployed: someone tightened the
# live KA in the console (numbered rules, "CITE EVERYTHING", "GIVE STEPS") and
# the constant was never updated.
#
# That drift had a measurable cost. A new KA built from the stale constant over
# byte-identical indexed content scored, across the 5 archetypes:
#     avg citations 1.2 vs 6.4, numbered steps 0/5 vs 3/5, Sources: line 0/5 vs 5/5
# The four numbered rules are what produce the cited, actionable answers the demo
# depends on — the polite paragraph form does not. Keep this in sync with the
# console, or a rebuild silently regresses answer quality.
KA_INSTRUCTIONS = (
    "You are a retrieval assistant over Fleetworthy (FIS) R&D troubleshooting "
    "tickets plus an acronym glossary. Follow these rules:\n\n"
    "1. CITE EVERYTHING. Put the source ticket number in parentheses after each "
    "claim, e.g. (R&DTASK0001033). End with a 'Sources:' line. Cite glossary.md "
    "for term definitions. Never state anything you can't ground in a retrieved "
    "source.\n"
    "2. GIVE STEPS. For any fix/diagnose intent, end with a numbered list of next "
    "steps, lowest-effort first, each grounded in a cited ticket.\n"
    "3. ASK FIRST IF UNCLEAR. If the request is ambiguous or missing key detail "
    "(site, equipment, symptom, what's been tried), ask 1-3 short clarifying "
    "questions before answering.\n"
    "4. HEDGE ON TERMS. For acronym/terminology questions, hedge (\"likely "
    "means...\") and corroborate across the glossary and multiple tickets, never "
    "one ticket alone.\n\n"
    "See the labeled Examples for the expected answer shape per question type."
)


# --- Reused helpers (mirror preflight/preflight.py) -------------------------

def run_cli(args, profile, timeout=180):
    """Run a databricks CLI command, return (exit_code, stdout, stderr)."""
    cmd = ["databricks", *args, "--profile", profile]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "databricks CLI not found"


def assert_target_host(profile):
    """HARD GATE — refuse to run against any workspace but the target."""
    code, out, err = run_cli(["auth", "env"], profile)
    if code != 0:
        print(
            f"FATAL: profile '{profile}' auth invalid ({err or 'auth env failed'}).",
            file=sys.stderr,
        )
        print(
            "Fix: databricks auth login --host "
            f"https://{TARGET_HOST_FRAGMENT}.cloud.databricks.com "
            f"--profile {profile}",
            file=sys.stderr,
        )
        sys.exit(2)
    try:
        env = json.loads(out).get("env", {})
    except json.JSONDecodeError:
        env = {}
    host = env.get("DATABRICKS_HOST", "")
    if TARGET_HOST_FRAGMENT not in host:
        print(
            f"FATAL: resolved host '{host}' is not the target "
            f"({TARGET_HOST_FRAGMENT}). Refusing to build the KA against the "
            "wrong workspace.",
            file=sys.stderr,
        )
        sys.exit(3)
    return host


def run_sql(statement, profile, warehouse_id=WAREHOUSE_ID):
    """Run a SQL statement on the serverless warehouse; return (state, data_array)."""
    payload = json.dumps(
        {
            "warehouse_id": warehouse_id,
            "statement": statement,
            "wait_timeout": "50s",
        }
    )
    code, out, err = run_cli(
        ["api", "post", "/api/2.0/sql/statements", "--json", payload], profile
    )
    if code != 0:
        return "CLI_ERROR", None
    try:
        d = json.loads(out)
        return (
            d.get("status", {}).get("state", "UNKNOWN"),
            d.get("result", {}).get("data_array"),
        )
    except json.JSONDecodeError:
        return "PARSE_ERROR", None


def api_json(method, path, profile, body=None, timeout=180):
    """Call `databricks api <method> <path>` with an optional JSON body.

    Returns (exit_code, parsed_json_or_None, raw_stdout, stderr).
    """
    args = ["api", method, path]
    if body is not None:
        args += ["--json", json.dumps(body)]
    code, out, err = run_cli(args, profile, timeout=timeout)
    parsed = None
    if out:
        try:
            parsed = json.loads(out)
        except json.JSONDecodeError:
            parsed = None
    return code, parsed, out, err


# --- Task 1: glossary Volume + upload ---------------------------------------

def ensure_glossary_volume(profile):
    """Create the MANAGED glossary Volume (idempotent) and upload glossary.md."""
    print("[T1] Ensuring glossary Volume exists...")
    ddl = (
        f"CREATE VOLUME IF NOT EXISTS "
        f"{DEMO_CATALOG}.{DEMO_SCHEMA}.{GLOSSARY_VOLUME} "
        f"COMMENT 'Curated FIS acronym glossary — 2nd KA knowledge source (D-03)'"
    )
    state, _ = run_sql(ddl, profile)
    if state != "SUCCEEDED":
        print(f"FATAL: CREATE VOLUME failed (state={state}).", file=sys.stderr)
        sys.exit(4)

    # Confirm via SHOW VOLUMES
    show_state, rows = run_sql(
        f"SHOW VOLUMES IN {DEMO_CATALOG}.{DEMO_SCHEMA}", profile
    )
    vol_names = {r[-1] if len(r) > 1 else r[0] for r in (rows or [])}
    print(f"[T1] Volumes in schema: {sorted(vol_names)}")

    if not LOCAL_GLOSSARY.exists():
        print(f"FATAL: {LOCAL_GLOSSARY} not found.", file=sys.stderr)
        sys.exit(4)

    print(f"[T1] Uploading glossary.md -> {GLOSSARY_FILE_PATH}")
    # `databricks fs cp` with overwrite; dbfs: scheme addresses /Volumes.
    code, out, err = run_cli(
        [
            "fs",
            "cp",
            str(LOCAL_GLOSSARY),
            f"dbfs:{GLOSSARY_FILE_PATH}",
            "--overwrite",
        ],
        profile,
    )
    if code != 0:
        print(f"FATAL: glossary upload failed: {err or out}", file=sys.stderr)
        sys.exit(4)

    # Verify presence
    code, out, err = run_cli(
        ["fs", "ls", f"dbfs:{GLOSSARY_VOLUME_PATH}"], profile
    )
    if code != 0 or "glossary.md" not in out:
        print(f"FATAL: glossary.md not found in Volume after upload: {out} {err}",
              file=sys.stderr)
        sys.exit(4)
    print("[T1] glossary.md present in Volume. OK")


# --- Task 2: create KA + attach two sources ---------------------------------

def find_existing_ka(profile):
    """Return the resource name (knowledge-assistants/{id}) of an existing KA
    with our display name, or None."""
    code, parsed, out, err = api_json(
        "get", "/api/2.1/knowledge-assistants", profile
    )
    if code != 0 or not isinstance(parsed, dict):
        return None
    for ka in parsed.get("knowledge_assistants", []) or []:
        if ka.get("display_name") == KA_DISPLAY_NAME:
            return ka.get("name") or f"knowledge-assistants/{ka.get('id')}"
    return None


def create_ka(profile):
    """Create the KA (idempotent by display name). Returns (name, id)."""
    existing = find_existing_ka(profile)
    if existing:
        print(f"[T2] KA already exists: {existing} (reusing)")
        code, parsed, _, _ = api_json("get", f"/api/2.1/{existing}", profile)
        ka_id = (parsed or {}).get("id")
        return existing, ka_id

    print("[T2] Creating Knowledge Assistant...")
    body = {
        "display_name": KA_DISPLAY_NAME,
        "description": (
            "Similar-case retrieval + terminology resolution over 223 FIS R&D "
            "troubleshooting tickets, with a curated acronym glossary source."
        ),
        "instructions": KA_INSTRUCTIONS,
    }
    code, parsed, out, err = api_json(
        "post", "/api/2.1/knowledge-assistants", profile, body=body
    )
    if code != 0 or not isinstance(parsed, dict):
        print(f"FATAL: KA create failed: {err or out}", file=sys.stderr)
        sys.exit(5)
    name = parsed.get("name") or f"knowledge-assistants/{parsed.get('id')}"
    ka_id = parsed.get("id")
    print(f"[T2] Created KA: name={name} id={ka_id} state={parsed.get('state')}")
    return name, ka_id


def list_sources(ka_name, profile):
    code, parsed, _, _ = api_json(
        "get", f"/api/2.1/{ka_name}/knowledge-sources", profile
    )
    if code != 0 or not isinstance(parsed, dict):
        return []
    return parsed.get("knowledge_sources", []) or []


def attach_sources(ka_name, profile):
    """Attach the Delta table + glossary Volume as two sources (idempotent)."""
    existing = list_sources(ka_name, profile)
    existing_names = {s.get("display_name") for s in existing}
    print(f"[T2] Existing sources: {sorted(n for n in existing_names if n)}")

    # Source 1 — Delta table (D-01). CONFIRMED source_type = "file_table".
    if "rnd_tickets_corpus" not in existing_names:
        print("[T2] Attaching source 1: rnd_tickets Delta table (file_table)...")
        body = {
            "display_name": "rnd_tickets_corpus",
            "description": "223 FIS R&D tickets; full case text in case_text.",
            "source_type": SOURCE_TYPE_FILE_TABLE,
            "file_table": {
                "table_name": CORPUS_TABLE,
                "file_col": CORPUS_CONTENT_COL,
            },
        }
        code, parsed, out, err = api_json(
            "post", f"/api/2.1/{ka_name}/knowledge-sources", profile, body=body
        )
        if code != 0:
            print(f"FATAL: Delta source attach failed: {err or out}", file=sys.stderr)
            sys.exit(5)
        print(f"[T2] Delta source attached: {(parsed or {}).get('name')}")
    else:
        print("[T2] Delta source already attached (skip).")

    # Source 2 — glossary Volume file (D-03). CONFIRMED source_type = "files".
    if "fis_glossary" not in existing_names:
        print("[T2] Attaching source 2: glossary Volume file (files)...")
        body = {
            "display_name": "fis_glossary",
            "description": "Curated FIS acronym glossary (CA=Controller Application, etc.).",
            "source_type": SOURCE_TYPE_FILES,
            # A1 spike (glossary leg): FilesSpec.path is a DIRECTORY UC volume path
            # ("a UC volume path that includes a list of files"), NOT a single file.
            # Passing the .md file itself returns NOT_FOUND — confirmed live.
            "files": {"path": GLOSSARY_VOLUME_PATH},
        }
        code, parsed, out, err = api_json(
            "post", f"/api/2.1/{ka_name}/knowledge-sources", profile, body=body
        )
        if code != 0:
            print(f"FATAL: glossary source attach failed: {err or out}", file=sys.stderr)
            sys.exit(5)
        print(f"[T2] Glossary source attached: {(parsed or {}).get('name')}")
    else:
        print("[T2] Glossary source already attached (skip).")

    sources = list_sources(ka_name, profile)
    print(f"[T2] Total sources attached: {len(sources)}")
    return sources


# --- ENR-03 repoint: detach + re-attach the corpus source at ka_content ------

def repoint_corpus_source(ka_name, profile):
    """Repoint the `rnd_tickets_corpus` source at the enriched `ka_content`
    column (ENR-03), using the DETACH + RE-ATTACH mechanism the 04.1-01 spike
    verdict recorded (REQUIRES_DETACH_REATTACH — `file_table.file_col` is
    IMMUTABLE in the knowledge-source UPDATE mask, so a :sync repoint is
    impossible; only DELETE + re-create moves the indexed column).

    Idempotent: if the corpus source already points at `ka_content`, do nothing.
    The glossary `files` source is left untouched. Returns True if a re-attach
    was performed (caller should then :sync + poll), False if already repointed.
    """
    sources = list_sources(ka_name, profile)
    corpus = next(
        (s for s in sources if s.get("display_name") == CORPUS_SOURCE_NAME), None)

    if corpus is not None:
        cur_col = (corpus.get("file_table") or {}).get("file_col")
        if cur_col == CORPUS_ENRICHED_COL:
            print(f"[REPOINT] {CORPUS_SOURCE_NAME} already points at "
                  f"'{CORPUS_ENRICHED_COL}' (skip — idempotent).")
            return False
        print(f"[REPOINT] {CORPUS_SOURCE_NAME} currently file_col='{cur_col}'. "
              f"Detaching (file_col is immutable — spike verdict "
              "REQUIRES_DETACH_REATTACH)...")
        code, parsed, out, err = api_json(
            "delete", f"/api/2.1/{corpus.get('name')}", profile)
        if code != 0:
            print(f"FATAL: could not detach {CORPUS_SOURCE_NAME}: {err or out}",
                  file=sys.stderr)
            sys.exit(7)
        print(f"[REPOINT] Detached {corpus.get('name')}.")
    else:
        print(f"[REPOINT] No existing {CORPUS_SOURCE_NAME} source — will attach "
              "fresh at the enriched column.")

    print(f"[REPOINT] Re-attaching {CORPUS_SOURCE_NAME} at "
          f"'{CORPUS_ENRICHED_COL}' (segmented + acronym-expanded content)...")
    body = {
        "display_name": CORPUS_SOURCE_NAME,
        "description": (
            "223 FIS R&D tickets; segmented + glossary-acronym-expanded content "
            "in ka_content (ENR-03). Citations resolve via the rnd_tickets "
            "metadata struct (ticket number in the file path)."
        ),
        "source_type": SOURCE_TYPE_FILE_TABLE,
        "file_table": {
            "table_name": CORPUS_TABLE,
            "file_col": CORPUS_ENRICHED_COL,
        },
    }
    code, parsed, out, err = api_json(
        "post", f"/api/2.1/{ka_name}/knowledge-sources", profile, body=body)
    if code != 0:
        print(f"FATAL: re-attach at {CORPUS_ENRICHED_COL} failed: {err or out}",
              file=sys.stderr)
        sys.exit(7)
    print(f"[REPOINT] Re-attached: {(parsed or {}).get('name')} -> "
          f"file_col={CORPUS_ENRICHED_COL}")
    return True


# --- Task 3: sync, poll, query, record --------------------------------------

def sync_sources(ka_name, profile):
    # Skip re-sync if everything is already UPDATED (avoids re-triggering a full
    # re-index on idempotent re-runs — each sync costs several minutes).
    srcs = list_sources(ka_name, profile)
    if srcs and all(s.get("state") == "UPDATED" for s in srcs):
        print("[T3] All sources already UPDATED — skipping :sync.")
        return
    print("[T3] Triggering :sync on knowledge sources...")
    code, parsed, out, err = api_json(
        "post", f"/api/2.1/{ka_name}/knowledge-sources:sync", profile, body={}
    )
    if code != 0:
        print(f"WARNING: :sync returned non-zero ({err or out}); "
              "continuing to poll (some builds auto-sync on attach).")
    else:
        print("[T3] :sync accepted.")


def poll_ready(ka_name, profile):
    """Poll until KA state == ACTIVE and each source state == UPDATED.

    Returns (ka_get_json, elapsed_seconds, ok_bool).
    """
    print("[T3] Polling KA readiness (interval "
          f"{POLL_INTERVAL_S}s, ceiling {POLL_CEILING_S//60} min)...")
    start = time.time()
    last = None
    while True:
        code, ka, _, _ = api_json("get", f"/api/2.1/{ka_name}", profile)
        srcs = list_sources(ka_name, profile)
        ka_state = (ka or {}).get("state", "UNKNOWN")
        src_states = {s.get("display_name"): s.get("state") for s in srcs}
        elapsed = int(time.time() - start)
        line = f"[T3] t+{elapsed}s KA={ka_state} sources={src_states}"
        if line != last:
            print(line)
            last = line

        if ka_state == "FAILED":
            print(f"FATAL: KA entered FAILED: {(ka or {}).get('error_info')}",
                  file=sys.stderr)
            return ka, elapsed, False
        if any(v == "FAILED_UPDATE" for v in src_states.values()):
            print(f"FATAL: a knowledge source FAILED_UPDATE: {src_states}",
                  file=sys.stderr)
            return ka, elapsed, False

        sources_updated = srcs and all(
            s.get("state") == "UPDATED" for s in srcs
        )
        if ka_state == "ACTIVE" and sources_updated:
            print(f"[T3] KA ACTIVE and all sources UPDATED in {elapsed}s.")
            return ka, elapsed, True

        if elapsed > POLL_CEILING_S:
            print(f"WARNING: exceeded {POLL_CEILING_S//60} min poll ceiling "
                  f"(KA={ka_state}, sources={src_states}). Reporting and stopping "
                  "the poll — re-run to resume polling.", file=sys.stderr)
            return ka, elapsed, False

        time.sleep(POLL_INTERVAL_S)


def _invoke_ka(endpoint_name, prompt, profile):
    """POST one prompt to the KA serving endpoint (Responses API shape)."""
    # KA serving endpoints use the Responses API shape ('input'), NOT the
    # chat-completions 'messages' field — confirmed live (the endpoint rejects
    # 'messages' with: "'messages' field is not supported. Please use 'input'").
    # NOTE: the `serving-endpoints query` CLI verb strips the `output[]` array
    # (returns only id/model/object); hit the /invocations REST path directly to
    # get the full response incl. citations. Confirmed live.
    body = {"input": [{"role": "user", "content": prompt}]}
    code, parsed, out, err = api_json(
        "post",
        f"/serving-endpoints/{endpoint_name}/invocations",
        profile,
        body=body,
        timeout=180,
    )
    if code != 0:
        return None, f"query failed (exit {code}): {(err or out)[:200]}"
    return parsed, out


def query_ka(endpoint_name, profile):
    """Fire two probes to capture BOTH citation-URL shapes for the 04-02 harness:
      (a) a similar-case query → expect TICKET-file citations (KA-01/02);
      (b) the A1 terminology query → expect a GLOSSARY citation + hedge (KA-03/04).
    Returns (primary_parsed, raw_combined_text). The similar-case response is the
    primary parsed object (it exercises the ticket-citation path the harness asserts)."""
    if not endpoint_name:
        return None, "no endpoint_name on KA"
    similar_prompt = (
        "What are the common issues and fixes for WIM (Weigh-In-Motion) systems "
        "reporting inflated or inaccurate weights? Cite the specific prior tickets."
    )
    term_prompt = (
        "In ticket R&DTASK0001006 the term \"CA\" is used repeatedly. "
        "What does \"CA\" most likely mean here? List the possible meanings "
        "and cite your sources."
    )
    print(f"[T3] Querying '{endpoint_name}' — probe A (similar-case, ticket citations)...")
    sim_parsed, sim_raw = _invoke_ka(endpoint_name, similar_prompt, profile)
    print(f"[T3] Querying '{endpoint_name}' — probe B (A1 terminology, glossary citation)...")
    term_parsed, term_raw = _invoke_ka(endpoint_name, term_prompt, profile)

    combined = (
        "===== PROBE A: similar-case (expect ticket-file citations) =====\n"
        f"prompt: {similar_prompt}\n\n{sim_raw}\n\n"
        "===== PROBE B: A1 terminology (expect glossary citation + hedge) =====\n"
        f"prompt: {term_prompt}\n\n{term_raw}\n"
    )
    # Prefer the probe that produced structured citations as the primary parsed obj.
    primary = sim_parsed if _has_citations(sim_parsed) else (term_parsed or sim_parsed)
    return primary, combined


def _has_citations(parsed):
    if not isinstance(parsed, dict):
        return False
    for item in parsed.get("output") or []:
        for c in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(c, dict) and c.get("annotations"):
                return True
    return False


def write_build_doc(host, ka_name, ka_id, ka_json, elapsed, ready,
                    sources, raw_query, query_parsed, query_note):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    endpoint_name = (ka_json or {}).get("endpoint_name")
    src_lines = []
    for s in sources:
        src_lines.append(
            f"| {s.get('display_name')} | `{s.get('source_type')}` | "
            f"{s.get('state')} | {s.get('name')} |"
        )

    # Derive a citation-shape summary from the raw query response.
    citation_summary = derive_citation_shape(query_parsed)

    raw_block = ""
    if raw_query:
        raw_block = raw_query if len(raw_query) < 8000 else raw_query[:8000] + "\n...[truncated]"

    doc = f"""# 04-KA-BUILD — Knowledge Assistant Build Record

**Generated:** {ts}
**Workspace:** `{host}`
**Built by:** `agents/build_ka.py` (Phase 4, Plan 01)

This file records the live-only KA build facts that Plan 04-02 (isolation harness)
and Phase 5 (Supervisor attach) consume: the KA identifiers, serving endpoint,
indexing wall-clock, the confirmed `source_type` wire string, and the
citation-payload shape.

## KA Identifiers (Phase 5 attaches these)

| Field | Value |
|-------|-------|
| display_name | `{KA_DISPLAY_NAME}` |
| resource name | `{ka_name}` |
| id | `{ka_id}` |
| serving endpoint_name | `{endpoint_name}` |
| final KA state | `{(ka_json or {}).get('state')}` |
| ready (ACTIVE + all UPDATED) | `{ready}` |

## Indexing Wall-Clock (Pitfall 6 — live-only)

- **Polled to ACTIVE/UPDATED in:** ~{elapsed}s ({elapsed/60:.1f} min) over 223 short
  `case_text` rows + 1 glossary file.
- Polling interval {POLL_INTERVAL_S}s; readiness gated on `state`, not a fixed timer.

## source_type Wire String (A1 spike — RESOLVED)

The research ASSUMED uppercase `FILE_TABLE`/`FILES`. The confirmed truth, read from
the `databricks-sdk>=0.122.0` `KnowledgeSource.source_type` docstring
("The type of the source: \\"index\\", \\"files\\", or \\"file_table\\""), is the
**LOWERCASE** strings:

| Source | Confirmed `source_type` | Spec block |
|--------|-------------------------|-----------|
| rnd_tickets Delta table | `file_table` | `file_table: {{table_name, file_col}}` |
| glossary Volume file | `files` | `files: {{path}}` |
| (VS index, unused) | `index` | `index: {{index_name, text_col, doc_uri_col}}` |

Both sources were accepted by `POST /api/2.1/{{name}}/knowledge-sources` with these
lowercase values on the first attempt — no fallback needed.

### Attached sources (live)

| display_name | source_type | state | resource name |
|--------------|-------------|-------|---------------|
{chr(10).join(src_lines) if src_lines else "| (none) | | | |"}

## Citation-Payload Shape (A2 spike — for the 04-02 harness)

Two probes were fired at the serving endpoint and the RAW responses inspected:
probe A = a similar-case query (expect **ticket-file** citations, KA-01/02);
probe B = the A1 terminology query "what does CA mean in R&DTASK0001006" (expect a
**glossary** citation + hedge, KA-03/04). The inspection below reflects the probe
that produced structured citations (raw block shows both).

{citation_summary}

**Query note:** {query_note}

<details>
<summary>Raw endpoint response (truncated to 8KB)</summary>

```json
{raw_block}
```
</details>

## REST Paths Used (verified from SDK 0.122.0 .do() calls)

- `POST /api/2.1/knowledge-assistants` — create KA
- `POST /api/2.1/{{name}}/knowledge-sources` — attach source (name = `knowledge-assistants/{{id}}`)
- `POST /api/2.1/{{name}}/knowledge-sources:sync` — trigger indexing
- `GET  /api/2.1/{{name}}` — poll state (`CREATING`→`ACTIVE`/`FAILED`)
- `GET  /api/2.1/{{name}}/knowledge-sources` — poll source state (`UPDATING`→`UPDATED`/`FAILED_UPDATE`)

No `/api/2.0/tiles` call is made anywhere (Anti-Pattern avoided).

## Reproduce

```bash
python3 agents/build_ka.py --profile serverless-stable
```
Idempotent: reuses an existing KA/sources/Volume by name.
"""
    BUILD_DOC.parent.mkdir(parents=True, exist_ok=True)
    BUILD_DOC.write_text(doc)
    print(f"[T3] Wrote {BUILD_DOC}")


def derive_citation_shape(query_parsed):
    """Inspect the raw KA response (Responses API shape) and describe how
    citations carry the ticket number + the exact parsing rule for the harness."""
    import re
    from urllib.parse import unquote
    TICKET_RE = re.compile(r"R(?:&|%26|&amp;)?DTASK(\d{7})")

    def extract_ticket(s):
        """Pull a ticket number from a possibly percent-encoded URL/string."""
        if not s:
            return None
        m = TICKET_RE.search(unquote(s))
        return f"R&DTASK{m.group(1)}" if m else None

    if not isinstance(query_parsed, dict):
        return ("The endpoint response could not be parsed as JSON this run; see the "
                "raw block below and re-capture. Expected the Responses-API shape: "
                "top-level `output[]`, each with `content[]`, each content item "
                "carrying `text` + `annotations[]`.")

    lines = ["Observed top-level keys: `" + "`, `".join(sorted(query_parsed.keys()))
             + "`. Shape = Databricks **Responses API** (`object: response`), "
             "NOT chat-completions."]

    # custom_outputs.sources_used is a quick did-it-ground-the-answer signal
    co = query_parsed.get("custom_outputs")
    if isinstance(co, dict) and "sources_used" in co:
        lines.append(f"- `custom_outputs.sources_used` = `{co.get('sources_used')}` "
                     "(top-level signal that retrieval grounded the answer).")

    # Walk output[].content[].annotations[]
    output = query_parsed.get("output") or []
    full_text_parts = []
    citations = []  # (title/url, extracted_ticket)
    ann_types = set()
    for item in output:
        for c in (item.get("content") or []) if isinstance(item, dict) else []:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    full_text_parts.append(c["text"])
                for ann in c.get("annotations") or []:
                    if isinstance(ann, dict):
                        ann_types.add(ann.get("type"))
                        url = ann.get("url") or ann.get("title") or ""
                        citations.append((url, extract_ticket(url)))

    full_text = "".join(full_text_parts)
    inline_tickets = sorted(set(re.findall(r"R&?DTASK\d{7}", full_text)))
    cited_tickets = sorted({t for _, t in citations if t})

    lines.append(
        "- **Answer text** is the concatenation of `output[].content[].text` "
        f"across all items ({len(output)} output item(s), "
        f"{len(full_text_parts)} text fragment(s))."
    )
    lines.append(
        "- **Citations are STRUCTURED**, carried per text-fragment in "
        f"`output[].content[].annotations[]` (annotation type(s): "
        f"`{'`, `'.join(sorted(t for t in ann_types if t))}`). Each annotation has "
        "`type: url_citation`, a `title` (the source file URL) and a `url` (deep-link "
        "with a `#:~:text=` fragment quoting the supporting sentence)."
    )
    if citations:
        ex_url, ex_tk = citations[0]
        lines.append(
            f"- **Ticket number is embedded in the citation URL path** "
            f"(`.../fs/files/synthetic/<NUMBER>.md`). Example → ticket `{ex_tk}` from:\n"
            f"  `{ex_url[:160]}...`"
        )
    lines.append(
        f"- Cited tickets this run (from annotation URLs): "
        f"{', '.join('`'+t+'`' for t in cited_tickets) if cited_tickets else '(none)'}."
    )
    lines.append(
        f"- Inline ticket tokens in prose (belt-and-suspenders): "
        f"{', '.join('`'+t+'`' for t in inline_tickets) if inline_tickets else '(none)'}."
    )

    lines.append(
        "\n**Recommended harness parsing rule (for 04-02):**\n"
        "1. POST to `/serving-endpoints/{endpoint}/invocations` with "
        "`{\"input\":[{\"role\":\"user\",\"content\": <question>}]}` "
        "(NOT `messages`; and NOT the `serving-endpoints query` CLI verb, which strips "
        "`output[]`).\n"
        "2. Prose answer = `''.join(c['text'] for o in resp['output'] "
        "for c in o.get('content',[]) if 'text' in c)`.\n"
        "3. Cited ticket numbers: **URL-decode** each `annotation['url']` first "
        "(the citation URL percent-encodes the ampersand as `R%26DTASK...`), THEN "
        "apply regex `R&?DTASK\\d{7}`. Annotations live under "
        "`output[].content[].annotations[]` with `type == 'url_citation'`. This is "
        "the authoritative citation carrier; the inline `(R&DTASKxxxxxxx)` tokens in "
        "the prose corroborate. NOTE: glossary-sourced citations point at "
        "`.../glossary/glossary.md` (no ticket number) — that is the correct carrier "
        "for acronym/terminology answers (KA-03/04).\n"
        "4. For KA-02 / Pitfall 3: assert each cited number ∈ corpus AND the cited "
        "ticket's `case_text` contains the claimed fact (the `#:~:text=` fragment in "
        "the URL quotes the exact supporting sentence — decode it to get the quote).\n"
        "5. `custom_outputs.sources_used == true` is a fast grounding pre-check."
    )
    return "\n".join(lines)



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
    ap = argparse.ArgumentParser(description="Build the FIS R&D Knowledge Assistant.")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--skip-query", action="store_true",
                    help="build + poll only; skip the live citation-spike query")
    ap.add_argument("--repoint", action="store_true",
                    help="ENR-03: detach + re-attach the corpus source at the "
                         "enriched ka_content column, then re-sync + poll "
                         "(per the 04.1-01 spike verdict REQUIRES_DETACH_REATTACH)")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    host = assert_target_host(args.profile)
    print(f"Host gate OK: {host}")

    if args.repoint:
        # ENR-03 repoint path — the KA + glossary Volume already exist; only
        # move the corpus source's indexed column to ka_content.
        ka_name = find_existing_ka(args.profile)
        if not ka_name:
            print(f"FATAL: no existing KA '{KA_DISPLAY_NAME}' to repoint.",
                  file=sys.stderr)
            sys.exit(7)
        code, parsed, _, _ = api_json("get", f"/api/2.1/{ka_name}", args.profile)
        ka_id = (parsed or {}).get("id")
        changed = repoint_corpus_source(ka_name, args.profile)
        if changed:
            sync_sources(ka_name, args.profile)
        ka_json, elapsed, ready = poll_ready(ka_name, args.profile)
        sources = list_sources(ka_name, args.profile)
        endpoint_name = (ka_json or {}).get("endpoint_name")
        query_parsed, raw_query, query_note = None, "", "skipped"
        if ready and not args.skip_query:
            query_parsed, raw_query = query_ka(endpoint_name, args.profile)
            query_note = ("live query fired (post-repoint)" if query_parsed
                          else f"query issue: {raw_query[:120]}")
        write_build_doc(
            host, ka_name, ka_id, ka_json, elapsed, ready, sources,
            raw_query, query_parsed, query_note,
        )
        if not ready:
            print("KA repoint did not reach ACTIVE/UPDATED this run — see "
                  "04-KA-BUILD.md.", file=sys.stderr)
            sys.exit(6)
        print(f"KA repoint complete: corpus source now indexes "
              f"'{CORPUS_ENRICHED_COL}', ACTIVE, all sources UPDATED.")
        return

    # Task 1
    ensure_glossary_volume(args.profile)

    # Task 2
    ka_name, ka_id = create_ka(args.profile)
    sources = attach_sources(ka_name, args.profile)

    # Task 3
    sync_sources(ka_name, args.profile)
    ka_json, elapsed, ready = poll_ready(ka_name, args.profile)
    sources = list_sources(ka_name, args.profile)  # refresh states

    endpoint_name = (ka_json or {}).get("endpoint_name")
    query_parsed, raw_query, query_note = None, "", "skipped"
    if ready and not args.skip_query:
        query_parsed, raw_query = query_ka(endpoint_name, args.profile)
        query_note = "live query fired" if query_parsed else f"query issue: {raw_query[:120]}"
        if not query_parsed:
            raw_query = raw_query or ""
    elif args.skip_query:
        query_note = "skipped (--skip-query)"
    elif not ready:
        query_note = "not fired — KA not ready (see indexing wall-clock)"

    write_build_doc(
        host, ka_name, ka_id, ka_json, elapsed, ready, sources,
        raw_query, query_parsed, query_note,
    )

    if not ready:
        print("KA build did not reach ACTIVE/UPDATED this run — see 04-KA-BUILD.md.",
              file=sys.stderr)
        sys.exit(6)
    print("KA build complete: ACTIVE, both sources UPDATED, build record written.")


if __name__ == "__main__":
    main()
