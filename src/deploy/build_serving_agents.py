#!/usr/bin/env python3
"""
Create a NEW Knowledge Assistant and a NEW Genie space, both over the single
`rd_tasks_serving` table (built by enrich/build_serving_table.py).

Why NEW assets instead of repointing the existing ones:
  * The KA's `file_col` is IMMUTABLE — moving the indexed column requires
    DELETE + re-create of the knowledge source, which forces a FULL re-index of
    the live demo's core retrieval path.
  * Side-by-side lets the old and new agents be compared on the same questions
    before anything is switched over.
The existing `fis-rnd-knowledge-assistant` and "FIS R&D Tickets" space are left
completely untouched.

What each new asset reads:
  * KA    -> rd_tasks_serving.ka_content        (+ the same glossary Volume file)
  * Genie -> rd_tasks_serving_analytics          (curated view over the same table)
So for the first time both engines resolve to the SAME physical rows.

Instructions/descriptions are IMPORTED from the existing builders rather than
retyped, so the only intended difference between old and new is the table.

Usage:
    python3 agents/build_serving_agents.py --profile serverless-stable --dry-run
    python3 agents/build_serving_agents.py --profile serverless-stable            # both
    python3 agents/build_serving_agents.py --profile serverless-stable --ka-only
    python3 agents/build_serving_agents.py --profile serverless-stable --genie-only
    python3 agents/build_serving_agents.py --profile serverless-stable --verify
"""

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "preflight"))
sys.path.insert(0, str(HERE))

from preflight import assert_target_host, run_sql  # noqa: E402
# Reuse the PROVEN builders' helpers + copy, so old/new differ only by table.
import build_ka as ka_mod  # noqa: E402
import build_genie as genie_mod  # noqa: E402
from build_ka import (  # noqa: E402
    KA_INSTRUCTIONS,
    POLL_INTERVAL_S,
    SOURCE_TYPE_FILE_TABLE,
    SOURCE_TYPE_FILES,
    api_json,
)

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
FQ = f"{CATALOG}.{SCHEMA}"
DEFAULT_PROFILE = "serverless-stable"

T_SERVING = f"{FQ}.rd_tasks_serving"
V_SERVING_ANALYTICS = f"{FQ}.rd_tasks_serving_analytics"
KA_CONTENT_COL = "ka_content"
EXPECTED_ROWS = 223

# New asset identities — deliberately distinct from the live ones.
NEW_KA_NAME = "fis-rnd-knowledge-assistant-serving"
NEW_KA_SOURCE = "rd_tasks_serving_corpus"
NEW_GENIE_TITLE = "FIS R&D Tickets (serving)"
GLOSSARY_SOURCE = "fis_glossary"

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
        print(f"FATAL: {T_SERVING} not found — run enrich/build_serving_table.py first.",
              file=sys.stderr)
        sys.exit(3)
    names = [r[0] for r in (cols or []) if r and r[0] and not r[0].startswith("#")]
    for required in ("metadata", KA_CONTENT_COL):
        if required not in names:
            print(f"FATAL: {T_SERVING} is missing '{required}' — the KA attach "
                  f"would fail.", file=sys.stderr)
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
        print(f"FATAL: {V_SERVING_ANALYTICS} not readable — run "
              f"enrich/build_serving_table.py first.", file=sys.stderr)
        sys.exit(3)
    print(f"[pre]   metadata ✓  {KA_CONTENT_COL} ✓  CDF ✓  analytics view ✓ ({r[0][0]} rows)")


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
                "Similar-case retrieval + terminology resolution over 223 FIS R&D "
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
                "223 FIS R&D tickets; segmented + glossary-acronym-expanded content "
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
            "description": "Curated FIS acronym glossary (CA=Controller Application, etc.).",
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
    """Attach the labeled examples from agents/ka_examples.json (idempotent).

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
        code, _, out, err = api_json(
            "post", f"/api/2.1/{ka_name}/examples", profile, body=body)
        if code != 0:
            print(f"WARNING: example attach failed ({err or out})")
            continue
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


# --- Genie ------------------------------------------------------------------

def build_genie(profile, dry_run=False):
    """Create the new space from the CURATED config, repointed at the serving view.

    The instructions/certified-queries in agents/genie_config.json are reused
    verbatim except for the table identifier, so the new space inherits the
    text-to-SQL steering (array-vs-text filter rules, glossary category handling)
    that the live space was tuned with.
    """
    cfg = json.load(open(HERE / "genie_config.json"))
    raw = json.dumps(cfg)
    n_before = raw.count("rd_tasks_gold_analytics")
    raw = raw.replace("rd_tasks_gold_analytics", "rd_tasks_serving_analytics")
    cfg = json.loads(raw)
    print(f"[Genie] Repointed {n_before} table reference(s) -> rd_tasks_serving_analytics")

    existing = genie_mod.find_existing_space(profile, NEW_GENIE_TITLE)

    if dry_run:
        print(f"\n[dry-run] Genie space '{NEW_GENIE_TITLE}'"
              + (f" (exists {existing} — would PATCH)" if existing else " (would CREATE)"))
        print(f"[dry-run]   table: {V_SERVING_ANALYTICS}")
        print(f"[dry-run]   warehouse: {WAREHOUSE_ID}")
        return None

    serialized = genie_mod.prepare_serialized_space(cfg)

    # Temporarily point the reused helpers at the NEW title/description so the
    # existing space is never touched.
    old_title, old_desc = genie_mod.SPACE_TITLE, genie_mod.SPACE_DESCRIPTION
    genie_mod.SPACE_TITLE = NEW_GENIE_TITLE
    genie_mod.SPACE_DESCRIPTION = (
        "FIS R&D task analytics over the consolidated rd_tasks_serving table — "
        "the SAME physical rows the Knowledge Assistant retrieves from. Counts, "
        "durations, expert-finding, priority triage and site-pattern analysis."
    )
    try:
        if existing:
            print(f"[Genie] Updating existing '{NEW_GENIE_TITLE}' ({existing})...")
            space_id = genie_mod.update_space(profile, existing, serialized)
        else:
            print(f"[Genie] Creating '{NEW_GENIE_TITLE}'...")
            space_id = genie_mod.create_space(profile, serialized)
    finally:
        genie_mod.SPACE_TITLE, genie_mod.SPACE_DESCRIPTION = old_title, old_desc

    # create_space/update_space return the FULL response body, not an id — pull the
    # id out rather than printing the whole serialized_space blob to the log.
    if isinstance(space_id, dict):
        space_id = space_id.get("space_id") or space_id.get("id")
    print(f"[Genie] space_id={space_id}")
    return space_id


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
        live_ka = find_ka(profile, "fis-rnd-knowledge-assistant")
        if live_ka:
            _, live, _, _ = api_json("get", f"/api/2.1/{live_ka}", profile)
            same = (ka or {}).get("instructions") == (live or {}).get("instructions")
            checks.append(("instructions MATCH the live KA (answer-shape parity)",
                           same, "identical" if same else "DIFFERENT"))

        # Examples parity — the instructions reference "the labeled Examples", so a
        # KA with none is under-specified relative to the live one.
        _, ex, _, _ = api_json("get", f"/api/2.1/{ka_name}/examples", profile)
        n_ex = len((ex or {}).get("examples", []))
        wanted = len(json.load(open(HERE / "ka_examples.json")))
        checks.append((f"examples attached == {wanted} (ka_examples.json)",
                       n_ex == wanted, str(n_ex)))

    # Look the space up by title via the raw API. find_existing_space() reads
    # genie_mod.SPACE_TITLE-independent args, but build_genie() restores the
    # module-level title in a finally block, so re-derive here from the constant
    # rather than relying on module state at verify time.
    space_id = find_space_by_title(profile, NEW_GENIE_TITLE)
    checks.append((f"Genie space '{NEW_GENIE_TITLE}' exists",
                   space_id is not None, str(space_id)))
    if space_id:
        tbl_ok = space_reads_table(profile, space_id, "rd_tasks_serving_analytics")
        checks.append(("Genie space reads rd_tasks_serving_analytics",
                       tbl_ok, "yes" if tbl_ok else "NO"))

    # The LIVE assets must be untouched.
    old_ka = find_ka(profile, "fis-rnd-knowledge-assistant")
    if old_ka:
        _, sp, _, _ = api_json("get", f"/api/2.1/{old_ka}/knowledge-sources", profile)
        old_srcs = (sp or {}).get("knowledge_sources", [])
        old_tbl = next((((s.get("file_table") or {}).get("table_name"))
                        for s in old_srcs
                        if s.get("display_name") == "rnd_tickets_corpus"), None)
        checks.append(("EXISTING KA still on rnd_tickets (untouched)",
                       old_tbl == f"{FQ}.rnd_tickets", str(old_tbl)))
    checks.append(("EXISTING Genie space 'FIS R&D Tickets' still present",
                   genie_mod.find_existing_space(profile, "FIS R&D Tickets") is not None,
                   "present"))

    ok = True
    for label, passed, got in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label} (got {got})")
        ok = ok and passed
    print()
    if not ok:
        print("VERIFY FAILED.", file=sys.stderr)
        sys.exit(6)
    print("VERIFY PASSED — new KA + Genie space serve from rd_tasks_serving; "
          "the existing assets are untouched.")



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
    ap = argparse.ArgumentParser(
        description="Create a new KA + Genie space over rd_tasks_serving.")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ka-only", action="store_true")
    ap.add_argument("--genie-only", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    assert_target_host(args.profile)  # host gate BEFORE any write

    if args.verify:
        verify(args.profile)
        return

    check_prereqs(args.profile)

    if not args.genie_only:
        build_ka(args.profile, dry_run=args.dry_run)
    if not args.ka_only:
        build_genie(args.profile, dry_run=args.dry_run)

    if not args.dry_run:
        print()
        verify(args.profile)


if __name__ == "__main__":
    main()
