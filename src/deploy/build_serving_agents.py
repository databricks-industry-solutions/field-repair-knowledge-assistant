#!/usr/bin/env python3
"""
Create the Knowledge Assistant over the single `rd_tasks_serving` table (built as a
plain Delta table by the `serving` notebook task of the rkb_data_pipeline job). The
KA is script-built because DAB has no native resource type for Agent Bricks
Knowledge Assistants.

Genie is NOT built here — it is deployed declaratively as a native DAB
`genie_spaces` resource (resources/genie.yml + genie/genie_space.json). This
script only READS the DAB-deployed Genie space during --verify (by title), to
confirm both engines resolve to the same physical rows:
  * KA    -> rd_tasks_serving.ka_content        (+ the same glossary Volume file)
  * Genie -> rd_tasks_serving_analytics          (DAB-deployed, curated view)

The KA `file_col` is IMMUTABLE, so a fresh KA is created rather than repointing an
existing one; any prior `rkb-knowledge-assistant` is left untouched.

Usage:
    python3 src/deploy/build_serving_agents.py --profile serverless-stable --dry-run
    python3 src/deploy/build_serving_agents.py --profile serverless-stable
    python3 src/deploy/build_serving_agents.py --profile serverless-stable --verify
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Serverless spark_python_task execs this file with no `__file__` and CWD = the
# script's own dir; fall back to CWD so paths resolve there and locally.
HERE = Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()
REPO = HERE.parent
sys.path.insert(0, str(HERE))  # preflight.py + env.py + build_ka.py live here

from preflight import assert_target_host, run_sql  # noqa: E402
# Reuse the PROVEN KA builder's helpers + copy, so the new KA differs only by table.
import build_ka as ka_mod  # noqa: E402
from build_ka import (  # noqa: E402
    KA_INSTRUCTIONS,
    POLL_INTERVAL_S,
    SOURCE_TYPE_FILE_TABLE,
    SOURCE_TYPE_FILES,
    api_json,
)

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical default values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
DEFAULT_PROFILE = "serverless-stable"

T_SERVING = f"{FQ}.rd_tasks_serving"
V_SERVING_ANALYTICS = f"{FQ}.rd_tasks_serving_analytics"
KA_CONTENT_COL = "ka_content"
EXPECTED_ROWS = int(os.environ.get("RKB_EXPECTED_ROWS", "0"))  # 0 = soft (non-empty only)

# New asset identities — deliberately distinct from the live ones.
NEW_KA_NAME = "rkb-knowledge-assistant-serving"
NEW_KA_SOURCE = "rd_tasks_serving_corpus"
NEW_GENIE_TITLE = "Field Repair Tickets (serving)"
GLOSSARY_SOURCE = "rkb_glossary"

POLL_CEILING_S = 60 * 90


# --- preconditions ----------------------------------------------------------

def check_prereqs(profile):
    """Fail fast if the serving table cannot back a KA / Genie space.

    These mirror the KA API's own attach-time checks (all three verified live as
    attach-blocking), so a misconfigured table is caught here rather than halfway
    through creating assets.
    """
    print("[pre] Checking rd_tasks_serving satisfies both engines...")
    st, cols = run_sql(f"DESCRIBE {T_SERVING}", profile, WAREHOUSE_ID)
    if st != "SUCCEEDED":
        print(f"FATAL: {T_SERVING} not found — run the rkb_data_pipeline job "
              f"(serving notebook) first.", file=sys.stderr)
        sys.exit(3)
    names = [r[0] for r in (cols or []) if r and r[0] and not r[0].startswith("#")]
    for required in ("metadata", KA_CONTENT_COL):
        if required not in names:
            print(f"FATAL: {T_SERVING} is missing '{required}' — the KA attach "
                  f"would fail.", file=sys.stderr)
            sys.exit(3)

    # rd_tasks_serving MUST be a real table, not a view/materialized view: the KA sync
    # STREAMS from it, and streaming from an MV fails (STREAMING_FROM_MATERIALIZED_VIEW)
    # even with CDF nominally set. This is the check whose absence shipped the MV regression.
    st, tt = run_sql(
        f"SELECT table_type FROM {CATALOG}.information_schema.tables "
        f"WHERE table_schema='{SCHEMA}' AND table_name='rd_tasks_serving'",
        profile, WAREHOUSE_ID)
    ttype = tt[0][0] if (st == "SUCCEEDED" and tt and tt[0]) else ""
    if str(ttype).upper() not in ("MANAGED", "EXTERNAL", "BASE TABLE", "MANAGED_TABLE"):
        print(f"FATAL: {T_SERVING} is '{ttype}', not a plain Delta table — the KA sync "
              f"streams from it and cannot stream from a view/materialized view.",
              file=sys.stderr)
        sys.exit(3)

    st, props = run_sql(f"SHOW TBLPROPERTIES {T_SERVING}", profile, WAREHOUSE_ID)
    cdf = any(r and "changeDataFeed" in r[0]
              and str(r[1]).lower() in ("true", "supported") for r in (props or []))
    if not cdf:
        print(f"FATAL: CDF not enabled on {T_SERVING} — the KA attach would fail "
              f"('must either be a streaming table or have Change Data Feed enabled').",
              file=sys.stderr)
        sys.exit(3)

    st, r = run_sql(f"SELECT count(*) FROM {V_SERVING_ANALYTICS}", profile, WAREHOUSE_ID)
    if st != "SUCCEEDED":
        print(f"FATAL: {V_SERVING_ANALYTICS} not readable — run the rkb_data_pipeline "
              f"job (serving notebook) first.", file=sys.stderr)
        sys.exit(3)
    print(f"[pre]   metadata ✓  {KA_CONTENT_COL} ✓  table-type ✓  CDF ✓  "
          f"analytics view ✓ ({r[0][0]} rows)")


# --- KA ---------------------------------------------------------------------

def find_ka(profile, display_name):
    code, parsed, _, _ = api_json("get", "/api/2.1/knowledge-assistants", profile)
    if code != 0 or not isinstance(parsed, dict):
        return None
    for ka in parsed.get("knowledge_assistants", []) or []:
        if ka.get("display_name") == display_name:
            return ka.get("name") or f"knowledge-assistants/{ka.get('id')}"
    return None


def build_ka(profile, dry_run=False):
    existing = find_ka(profile, NEW_KA_NAME)
    if dry_run:
        print(f"\n[dry-run] KA '{NEW_KA_NAME}'"
              + (" (already exists — would reuse)" if existing else " (would CREATE)"))
        print(f"[dry-run]   source 1: {T_SERVING} file_col={KA_CONTENT_COL}")
        print(f"[dry-run]   source 2: {ka_mod.GLOSSARY_FILE_PATH}")
        print(f"[dry-run]   then :sync + poll to ACTIVE/UPDATED")
        return None

    if existing:
        print(f"[KA] Reusing existing {NEW_KA_NAME}: {existing}")
        ka_name = existing
        # Instructions are what drive citation/steps behaviour, and a reused KA may
        # be carrying an older revision. PATCH them so a re-run converges on the
        # canonical text instead of silently keeping whatever it was created with.
        code, cur, _, _ = api_json("get", f"/api/2.1/{ka_name}", profile)
        if (cur or {}).get("instructions") != KA_INSTRUCTIONS:
            print("[KA] Instructions differ from build_ka.KA_INSTRUCTIONS — updating...")
            code, _, out, err = api_json(
                "patch", f"/api/2.1/{ka_name}?update_mask=instructions", profile,
                body={"instructions": KA_INSTRUCTIONS})
            if code != 0:
                print(f"WARNING: instruction update failed ({err or out}); the KA "
                      "keeps its previous instructions.")
            else:
                print("[KA] Instructions updated.")
        else:
            print("[KA] Instructions already match (skip).")
    else:
        print(f"[KA] Creating '{NEW_KA_NAME}'...")
        body = {
            "display_name": NEW_KA_NAME,
            "description": (
                "Similar-case retrieval + terminology resolution over R&D "
                "troubleshooting tickets, reading the consolidated rd_tasks_serving "
                "table (the SAME physical rows Genie queries), with a curated "
                "acronym glossary source."
            ),
            # Same instructions as the live KA — the table is the only difference.
            "instructions": KA_INSTRUCTIONS,
        }
        code, parsed, out, err = api_json(
            "post", "/api/2.1/knowledge-assistants", profile, body=body)
        if code != 0 or not isinstance(parsed, dict):
            print(f"FATAL: KA create failed: {err or out}", file=sys.stderr)
            sys.exit(5)
        ka_name = parsed.get("name") or f"knowledge-assistants/{parsed.get('id')}"
        print(f"[KA] Created: {ka_name} state={parsed.get('state')}")

    # --- sources (idempotent by display_name) ---
    code, parsed, _, _ = api_json("get", f"/api/2.1/{ka_name}/knowledge-sources", profile)
    have = {s.get("display_name") for s in (parsed or {}).get("knowledge_sources", [])}

    if NEW_KA_SOURCE not in have:
        print(f"[KA] Attaching corpus: {T_SERVING} (file_col={KA_CONTENT_COL})...")
        body = {
            "display_name": NEW_KA_SOURCE,
            "description": (
                "R&D tickets; segmented + glossary-acronym-expanded content "
                "in ka_content on the consolidated rd_tasks_serving table. Citations "
                "resolve via the metadata struct (ticket number in the file path). "
                "Genie reads the structured columns of this SAME table."
            ),
            "source_type": SOURCE_TYPE_FILE_TABLE,
            "file_table": {"table_name": T_SERVING, "file_col": KA_CONTENT_COL},
        }
        code, parsed, out, err = api_json(
            "post", f"/api/2.1/{ka_name}/knowledge-sources", profile, body=body)
        if code != 0:
            print(f"FATAL: corpus attach failed: {err or out}", file=sys.stderr)
            sys.exit(5)
        print(f"[KA]   attached: {(parsed or {}).get('name')}")
    else:
        print(f"[KA] Corpus source already attached (skip).")

    if GLOSSARY_SOURCE not in have:
        print(f"[KA] Attaching glossary: {ka_mod.GLOSSARY_VOLUME_PATH}...")
        body = {
            "display_name": GLOSSARY_SOURCE,
            "description": "Curated acronym glossary (CA=Controller Application, etc.).",
            "source_type": SOURCE_TYPE_FILES,
            # FilesSpec.path is a DIRECTORY volume path, NOT a single file. Passing
            # the .md file itself returns NOT_FOUND — documented in build_ka.py from
            # the A1 spike, and reconfirmed here by making exactly that mistake.
            "files": {"path": ka_mod.GLOSSARY_VOLUME_PATH},
        }
        code, parsed, out, err = api_json(
            "post", f"/api/2.1/{ka_name}/knowledge-sources", profile, body=body)
        if code != 0:
            # Fail loudly: without the glossary the KA loses terminology
            # resolution, so it is NOT equivalent to the live KA it is meant to
            # be compared against.
            print(f"FATAL: glossary attach failed: {err or out}", file=sys.stderr)
            print("The new KA would have only the corpus source and could not be "
                  "compared fairly against the live 2-source KA.", file=sys.stderr)
            sys.exit(5)
        print(f"[KA]   attached: {(parsed or {}).get('name')}")
    else:
        print("[KA] Glossary source already attached (skip).")

    attach_examples(ka_name, profile)
    sync_and_poll(ka_name, profile)
    return ka_name


def attach_examples(ka_name, profile):
    """Attach the labeled examples from src/deploy/ka_examples.json (idempotent).

    These matter more than they look: the instructions END with "See the labeled
    Examples for the expected answer shape per question type", so a KA with zero
    examples is being told to consult guidance that does not exist. The live KA has
    8; the first build of this KA had none, which (together with the stale
    instructions) is why its answers were shorter and cited less.
    """
    code, parsed, _, _ = api_json("get", f"/api/2.1/{ka_name}/examples", profile)
    have = {(e.get("question") or "").strip()
            for e in (parsed or {}).get("examples", [])}
    if have:
        print(f"[KA] {len(have)} example(s) already attached.")

    wanted = json.load(open(HERE / "ka_examples.json"))
    added = 0
    for ex in wanted:
        q = (ex.get("question") or "").strip()
        if not q or q in have:
            continue
        body = {"question": q, "guidelines": [ex.get("guideline") or ""]}
        # The example endpoint is LLM-backed and intermittently slow (5-min client
        # timeouts observed), so retry each attach a few times with backoff rather than
        # letting one transient timeout drop an example.
        ok = False
        for attempt in range(1, 4):
            code, _, out, err = api_json(
                "post", f"/api/2.1/{ka_name}/examples", profile, body=body)
            if code == 0:
                ok = True
                break
            print(f"WARNING: example attach failed (attempt {attempt}/3): {err or out}")
            time.sleep(5 * attempt)
        if ok:
            added += 1
    print(f"[KA] Examples: {added} added, {len(have)} pre-existing "
          f"({len(wanted)} in ka_examples.json).")


def sync_and_poll(ka_name, profile):
    code, parsed, _, _ = api_json("get", f"/api/2.1/{ka_name}/knowledge-sources", profile)
    srcs = (parsed or {}).get("knowledge_sources", [])
    if srcs and all(s.get("state") == "UPDATED" for s in srcs):
        print("[KA] All sources already UPDATED — skipping :sync.")
        return True

    print("[KA] Triggering :sync...")
    code, _, out, err = api_json(
        "post", f"/api/2.1/{ka_name}/knowledge-sources:sync", profile, body={})
    if code != 0:
        print(f"WARNING: :sync non-zero ({err or out}); polling anyway.")

    print(f"[KA] Polling to ACTIVE/UPDATED (interval {POLL_INTERVAL_S}s)...")
    start = time.time()
    last = None
    # An API/auth failure must NOT be reported as a KA state. A previous run spun
    # for 8 minutes printing "KA=UNKNOWN sources={}" while the real cause was an
    # expired OAuth refresh token — the poll was blind, not the KA broken. Treat
    # consecutive request failures as fatal and say why.
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 3
    while True:
        ka_code, ka, ka_out, ka_err = api_json("get", f"/api/2.1/{ka_name}", profile)
        sp_code, sp, _, sp_err = api_json(
            "get", f"/api/2.1/{ka_name}/knowledge-sources", profile)
        el = int(time.time() - start)

        if ka_code != 0 or sp_code != 0:
            consecutive_errors += 1
            detail = (ka_err or sp_err or ka_out or "").strip()[:300]
            print(f"[KA] t+{el}s REQUEST FAILED "
                  f"({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {detail}",
                  flush=True)
            if "refresh token is invalid" in detail.lower() or "oidc" in detail.lower():
                print("FATAL: the CLI OAuth token expired mid-poll. The KA itself is "
                      "probably still indexing — re-authenticate and re-run with "
                      "--verify (the build is idempotent):\n"
                      f"  databricks auth login --profile {profile}",
                      file=sys.stderr)
                return False
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                print(f"FATAL: {consecutive_errors} consecutive API failures while "
                      f"polling — aborting rather than reporting a fake state.",
                      file=sys.stderr)
                return False
            time.sleep(POLL_INTERVAL_S)
            continue
        consecutive_errors = 0

        srcs = (sp or {}).get("knowledge_sources", [])
        ka_state = (ka or {}).get("state", "UNKNOWN")
        states = {s.get("display_name"): s.get("state") for s in srcs}
        line = f"[KA] t+{el}s KA={ka_state} sources={states}"
        if line != last:
            print(line, flush=True)
            last = line
        if ka_state == "FAILED":
            print(f"FATAL: KA FAILED: {(ka or {}).get('error_info')}", file=sys.stderr)
            return False
        if any(v == "FAILED_UPDATE" for v in states.values()):
            print(f"FATAL: source FAILED_UPDATE: {states}", file=sys.stderr)
            return False
        if ka_state == "ACTIVE" and srcs and all(
                s.get("state") == "UPDATED" for s in srcs):
            print(f"[KA] ACTIVE, all sources UPDATED in {el}s.")
            return True
        if el > POLL_CEILING_S:
            print(f"WARNING: ceiling {POLL_CEILING_S}s exceeded; last={states}")
            return False
        time.sleep(POLL_INTERVAL_S)


# --- verify -----------------------------------------------------------------

def find_space_by_title(profile, title):
    """space_id for a Genie space with this exact title, else None."""
    code, parsed, _, _ = api_json("get", "/api/2.0/genie/spaces", profile)
    if code != 0 or not isinstance(parsed, dict):
        return None
    for s in parsed.get("spaces", []) or []:
        if (s.get("title") or "") == title:
            return s.get("space_id")
    return None


def space_reads_table(profile, space_id, table_suffix):
    """True if the space's serialized_space references the given table.

    NOTE the query param: a plain GET omits `serialized_space` entirely (it returns
    only description/parent_path/space_id/title/warehouse_id), so the config must be
    requested explicitly or this check silently reports "no table".
    """
    code, parsed, _, _ = api_json(
        "get", f"/api/2.0/genie/spaces/{space_id}?include_serialized_space=true",
        profile)
    if code != 0 or not isinstance(parsed, dict):
        return False
    return table_suffix in (parsed.get("serialized_space") or "")


def verify(profile):
    print("[verify] new serving-backed agents\n")
    checks = []

    ka_name = find_ka(profile, NEW_KA_NAME)
    checks.append((f"KA '{NEW_KA_NAME}' exists", ka_name is not None, str(ka_name)))

    if ka_name:
        _, ka, _, _ = api_json("get", f"/api/2.1/{ka_name}", profile)
        state = (ka or {}).get("state")
        checks.append(("KA state == ACTIVE", state == "ACTIVE", str(state)))

        _, sp, _, _ = api_json("get", f"/api/2.1/{ka_name}/knowledge-sources", profile)
        srcs = (sp or {}).get("knowledge_sources", [])
        corpus = next((s for s in srcs if s.get("display_name") == NEW_KA_SOURCE), None)
        tbl = ((corpus or {}).get("file_table") or {}).get("table_name")
        col = ((corpus or {}).get("file_table") or {}).get("file_col")
        checks.append(("KA corpus points at rd_tasks_serving",
                       tbl == T_SERVING, str(tbl)))
        checks.append((f"KA file_col == {KA_CONTENT_COL}",
                       col == KA_CONTENT_COL, str(col)))
        checks.append(("all KA sources UPDATED",
                       bool(srcs) and all(s.get("state") == "UPDATED" for s in srcs),
                       str({s.get("display_name"): s.get("state") for s in srcs})))

        # Instruction parity with the LIVE KA. This is the check that would have
        # caught the real cause of the old-vs-new answer gap: identical indexed
        # content, different instructions -> 1.2 vs 6.4 avg citations.
        live_ka = find_ka(profile, "rkb-knowledge-assistant")
        if live_ka:
            _, live, _, _ = api_json("get", f"/api/2.1/{live_ka}", profile)
            same = (ka or {}).get("instructions") == (live or {}).get("instructions")
            checks.append(("instructions MATCH the live KA (answer-shape parity)",
                           same, "identical" if same else "DIFFERENT"))

        # Examples parity — the instructions reference "the labeled Examples", so a
        # KA with none is under-specified relative to the live one. The example endpoint
        # is LLM-backed and intermittently times out, so a PARTIAL attach is a soft
        # WARNING (the KA is ACTIVE and functional; re-run to fill the rest) — only ZERO
        # examples is a hard FAIL. attach_examples is idempotent, so re-running converges.
        _, ex, _, _ = api_json("get", f"/api/2.1/{ka_name}/examples", profile)
        n_ex = len((ex or {}).get("examples", []))
        wanted = len(json.load(open(HERE / "ka_examples.json")))
        if n_ex == wanted:
            checks.append((f"examples attached == {wanted} (ka_examples.json)", True, str(n_ex)))
        elif n_ex >= 1:
            print(f"  [WARN] examples attached {n_ex}/{wanted} — the example endpoint "
                  f"timed out on some; the KA is ACTIVE and answers. Re-run this task to "
                  f"attach the rest (idempotent).")
        else:
            checks.append(("examples attached >= 1 (ka_examples.json)", False, str(n_ex)))

    # The Genie space is DAB-deployed (resources/genie.yml); look it up by title to
    # confirm it exists and reads the serving analytics view. This script does not
    # create or modify it — read-only cross-check that both engines see the same rows.
    space_id = find_space_by_title(profile, NEW_GENIE_TITLE)
    checks.append((f"DAB Genie space '{NEW_GENIE_TITLE}' exists",
                   space_id is not None, str(space_id)))
    if space_id:
        tbl_ok = space_reads_table(profile, space_id, "rd_tasks_serving_analytics")
        checks.append(("Genie space reads rd_tasks_serving_analytics",
                       tbl_ok, "yes" if tbl_ok else "NO"))

    # Any pre-existing legacy KA must be untouched. NOTE: this KA (unsuffixed
    # `rkb-knowledge-assistant`) may live in a DIFFERENT schema than this deploy —
    # e.g. the shared demo `rkb_knowledge_agent` while we build into `rkb_knowledge_agent_dev`.
    # The invariant is simply that it is still on ITS OWN `rnd_tickets` corpus (we didn't
    # repoint it), so assert the table name ends in `.rnd_tickets` rather than pinning it to
    # this deploy's schema (which would false-fail on any isolated/suffixed deploy).
    old_ka = find_ka(profile, "rkb-knowledge-assistant")
    if old_ka:
        _, sp, _, _ = api_json("get", f"/api/2.1/{old_ka}/knowledge-sources", profile)
        old_srcs = (sp or {}).get("knowledge_sources", [])
        old_tbl = next((((s.get("file_table") or {}).get("table_name"))
                        for s in old_srcs
                        if s.get("display_name") == "rnd_tickets_corpus"), None)
        checks.append(("EXISTING legacy KA still on its rnd_tickets (untouched)",
                       bool(old_tbl) and str(old_tbl).endswith(".rnd_tickets"), str(old_tbl)))

    ok = True
    for label, passed, got in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} (got {got})")
        ok = ok and passed
    print()
    if not ok:
        print("VERIFY FAILED.", file=sys.stderr)
        sys.exit(6)
    print("VERIFY PASSED — the KA serves from rd_tasks_serving and the DAB Genie "
          "space reads the same rows.")



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
    # Suffix the KA display name so a dev/isolated deploy builds its OWN Knowledge
    # Assistant instead of reusing the shared demo KA (which points at the demo schema).
    suffix = getattr(args, "agent_suffix", "") or ""
    if suffix:
        g["NEW_KA_NAME"] = g["NEW_KA_NAME"] + suffix
    # Retarget the imported build_ka module too, so its glossary Volume paths point at
    # THIS schema (build_ka's globals are separate from this module's).
    ka_mod._apply_target(args)
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
        description="Create the Knowledge Assistant over rd_tasks_serving "
                    "(Genie is deployed declaratively via DAB, not here).")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--agent-suffix", default="",
                    help="Suffix for the KA display name so a dev/isolated deploy gets "
                         "its own KA (e.g. '-dev'), instead of reusing the shared demo KA.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    assert_target_host(args.profile)  # host gate BEFORE any write

    if args.verify:
        verify(args.profile)
        return

    check_prereqs(args.profile)
    build_ka(args.profile, dry_run=args.dry_run)

    if not args.dry_run:
        print()
        verify(args.profile)


if __name__ == "__main__":
    main()
