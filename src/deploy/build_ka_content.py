#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 4.1 Plan 05, Task 1: build the segmented,
glossary-acronym-expanded KA content column (`ka_content`).

This is the enriched content column the repointed Knowledge Assistant indexes
instead of raw `case_text` (ENR-03). It is:

  * SEGMENTED — built from the ENR-02 meaning-segmented gold columns
    (summary / troubleshooting / root_cause / resolution), concatenated with
    newlines, instead of the raw ticket blob. Retrieval hits the part of the
    case a query is actually about.
  * ACRONYM-EXPANDED — the FIRST occurrence of every approved-glossary acronym
    (`term` matching `[A-Z0-9]{2,6}`) is expanded inline to
    `ACR (short definition)` so KA embeds the disambiguated form
    (e.g. `AUR` -> `AUR (Camera unit that reads USDOT numbers ...)`). The
    expansion map is built AT RUN TIME from the approved glossary — there is NO
    hardcoded acronym list anywhere (GLO-02 coupling; mirrors the reference
    `normalize_text` UDF, 50_search.py lines 55-68).

WHERE the column lives (correctness decision, deviation-worthy — see SUMMARY):
  `ka_content` is added to `rnd_tickets`, NOT to `rd_tasks_gold_enrichment`.
  KA citation URLs are built from the source table's `metadata` STRUCT
  (`file_path` -> `.../synthetic/<NUMBER>.md`), which carries the ticket number.
  Only `rnd_tickets` has that struct (and CDF enabled); `rd_tasks_gold_enrichment`
  does not. Putting `ka_content` on `rnd_tickets` keeps the reattached KA source
  citing correctly (test_ka.py KA-02) — the hard must-have. `rnd_tickets` is
  effectively the "companion table keyed on number" the plan permits, and it
  already carries the metadata struct + CDF the KA source needs.

No Spark session — every stage is a SQL statement issued through `run_sql`
(host-gated via preflight `assert_target_host`). The acronym expansion is
compiled to a chain of `regexp_replace(...)` SQL expressions (one per acronym,
first-occurrence only via `(?s)^(.*?)\\bACR\\b` -> `$1ACR (def)`), bounded by the
~two dozen approved acronyms. Idempotent: re-run recomputes `ka_content` via MERGE.

Usage:
    python3 enrich/build_ka_content.py --profile serverless-stable
    python3 enrich/build_ka_content.py --profile serverless-stable --verify
"""

import argparse
import re
import sys
from pathlib import Path

# --- Reuse the Phase 1 host-safety gate + SQL runner ------------------------
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "preflight"))
from preflight import assert_target_host, run_sql  # noqa: E402
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
T_TICKETS = f"{FQ}.rnd_tickets"          # KA source table (has metadata struct + CDF)
T_GOLD_ENRICH = f"{FQ}.rd_tasks_gold_enrichment"
T_GLOSSARY = f"{FQ}.glossary"
DEFAULT_PROFILE = "serverless-stable"

KA_CONTENT_COL = "ka_content"
# The ENR-02 meaning-segmented columns that compose the KA content (reference
# 50_search.py: KA content = concat of normalize_text-expanded segments).
SEGMENT_COLS = ["summary", "troubleshooting", "root_cause", "resolution"]

# An acronym is a glossary term head matching this (mirrors normalize_text ACR).
ACR_RE = re.compile(r"^[A-Z0-9]{2,6}$")
SHORTDEF_MAX = 60


# --- Build the acronym -> short-definition map FROM the approved glossary ----

def load_acronym_map(profile):
    """{acronym: short_def} for approved glossary terms matching [A-Z0-9]{2,6}.

    NO hardcoded acronym list — this is GLO-02: an acronym is expandable iff it
    is an approved glossary term. Short def = first clause of the definition,
    mirroring the reference `normalize_text` UDF derivation.
    """
    st, rows = run_sql(
        f"SELECT term, definition FROM {T_GLOSSARY} WHERE status='approved'",
        profile, WAREHOUSE_ID)
    if st != "SUCCEEDED" or rows is None:
        print(f"FATAL: could not load approved glossary (state={st}).",
              file=sys.stderr)
        sys.exit(4)
    acr = {}
    for term, definition in rows:
        head = (term or "").split(" / ")[0]
        if not ACR_RE.match(head):
            continue
        # First clause: split off the first sentence / em-dash / parenthetical,
        # collapse whitespace, cap length. Matches 50_search.py's derivation.
        short = (definition or "").split(".")[0].split(" — ")[0].split(" (")[0]
        short = re.sub(r"\s+", " ", short).strip()[:SHORTDEF_MAX].strip()
        if short:
            acr[head] = short
    return acr


def _sql_str(s):
    """Escape a Python string for a single-quoted Spark SQL string literal."""
    return s.replace("\\", "\\\\").replace("'", "''")


def _sql_replacement(acr, short):
    """Build the regexp_replace replacement string `$1$2ACR (short)$3`.

    Group refs: $1 = the (.*?) prefix, $2 = the left boundary char (or empty at
    start), $3 = the right boundary char (or empty at end). In the replacement,
    `$` and `\\` are special, so any literal `$`/`\\` from the definition text is
    escaped so it is emitted literally rather than parsed as a group ref/escape.
    """
    safe_short = short.replace("\\", "\\\\").replace("$", "\\$")
    return f"$1$2{acr} ({safe_short})$3"


def decascade(acr_map):
    """Make the expansion chain order-independent by lower-casing cross-references.

    The expansions are applied as a CHAIN of regexp_replace, so text inserted by
    an earlier link is still visible to later ones. Four approved definitions name
    another acronym (PIPS->ALPR, CA->HTS, GOBI->ATIS, DW->SRIS), which produced
    nested garbage in the indexed content:

        PIPS (Vendor(s) associated with ALPR (Capability) cameras and readers)
                                            ^^^^^^^^^^^^ injected by a later link

    The patterns are case-SENSITIVE, so lower-casing a cross-referenced token
    ("ALPR" -> "alpr") makes it unmatchable by every later link while KEEPING the
    word: the definition still reads correctly, unlike deleting the token, which
    left dangling text ("A type or brand of camera used in").

    Only affects the DEFINITION text. Acronyms in the ticket prose still expand
    exactly once, and the glossary itself is untouched (GLO-02 intact).
    """
    others = set(acr_map)
    cleaned = {}
    for acr, short in acr_map.items():
        text = short
        for other in others:
            if other == acr:
                continue
            text = re.sub(
                rf"(^|[^A-Za-z0-9]){other}([^A-Za-z0-9]|$)",
                lambda m: m.group(1) + other.lower() + m.group(2),
                text,
            )
        cleaned[acr] = text
    return cleaned


def expand_expr(col, acr_map):
    """Compile a chain of regexp_replace() over `col`, expanding the FIRST
    occurrence of each acronym to `ACR (short def)`.

    Pattern `(?s)^(.*?)(^|[^A-Za-z0-9])ACR([^A-Za-z0-9]|$)` matches from the start
    of the (dotall) text through the first STANDALONE ACR token (bounded by a
    non-alphanumeric char or a string edge — a backslash-free word boundary that
    survives the JSON/SQL-literal round-trip cleanly). The replacement re-emits
    the prefix + boundary chars, then the expanded form -> first-occurrence only,
    no false matches inside longer words (e.g. AUR inside AURORA).

    Definitions are de-cascaded first so an inserted expansion cannot itself be
    expanded by a later link in the chain (see decascade).
    """
    expr = col
    for acr, short in decascade(acr_map).items():
        pattern = f"(?s)^(.*?)(^|[^A-Za-z0-9]){acr}([^A-Za-z0-9]|$)"
        repl = _sql_replacement(acr, short)
        expr = (f"regexp_replace({expr}, '{_sql_str(pattern)}', "
                f"'{_sql_str(repl)}')")
    return expr


def build_ka_content_select(acr_map):
    """The `SELECT number, <ka_content>` that composes the enriched content."""
    parts = [expand_expr(f"coalesce({c}, '')", acr_map) for c in SEGMENT_COLS]
    concat = "concat_ws('\\n', " + ", ".join(parts) + ")"
    return (f"SELECT number, {concat} AS {KA_CONTENT_COL} "
            f"FROM {T_GOLD_ENRICH}")


# --- runners ----------------------------------------------------------------

def run_or_die(label, stmt, profile, poll=False):
    runner = run_sql_poll if poll else run_sql
    state, data = runner(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED":
        print(f"FATAL: {label} failed (state={state}).", file=sys.stderr)
        print(stmt[:2000], file=sys.stderr)
        sys.exit(4)
    print(f"[ka-content] {label}: SUCCEEDED")
    return data


def scalar(stmt, profile):
    state, data = run_sql(stmt, profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not data:
        return None, state
    return data[0][0], state


def build(profile):
    print("[ka-content] Step 1: build acronym map FROM approved glossary (GLO-02)...")
    acr_map = load_acronym_map(profile)
    print(f"[ka-content]   {len(acr_map)} acronyms: {sorted(acr_map)}")
    if not acr_map:
        print("FATAL: no approved acronyms in glossary — cannot expand content.",
              file=sys.stderr)
        sys.exit(4)

    print(f"[ka-content] Step 2: ensure {KA_CONTENT_COL} column on rnd_tickets...")
    # ADD COLUMN IF NOT EXISTS is not supported for a single column on Delta;
    # probe the schema and ADD only when absent (keeps the build idempotent).
    st, cols = run_sql(f"DESCRIBE {T_TICKETS}", profile, WAREHOUSE_ID)
    have_col = any(r and r[0] == KA_CONTENT_COL for r in (cols or []))
    if have_col:
        print(f"[ka-content]   {KA_CONTENT_COL} column already present (skip ADD).")
    else:
        run_or_die(
            f"ALTER TABLE ADD {KA_CONTENT_COL}",
            f"ALTER TABLE {T_TICKETS} ADD COLUMN {KA_CONTENT_COL} STRING "
            f"COMMENT 'Segmented + glossary-acronym-expanded KA content (ENR-03). "
            f"Built by enrich/build_ka_content.py from the ENR-02 gold segments.'",
            profile)

    print("[ka-content] Step 3: MERGE segmented+expanded content into rnd_tickets...")
    select_sql = build_ka_content_select(acr_map)
    merge = (
        f"MERGE INTO {T_TICKETS} t USING ({select_sql}) s "
        f"ON t.number = s.number "
        f"WHEN MATCHED THEN UPDATE SET t.{KA_CONTENT_COL} = s.{KA_CONTENT_COL}"
    )
    run_or_die("MERGE ka_content", merge, profile, poll=True)

    # Report
    n_rows, _ = scalar(f"SELECT count(*) FROM {T_TICKETS} "
                       f"WHERE {KA_CONTENT_COL} IS NOT NULL "
                       f"AND length({KA_CONTENT_COL}) > 0", profile)
    print(f"[ka-content] {KA_CONTENT_COL} populated on {n_rows} rows.")


def verify(profile):
    print("[ka-content] --verify: acceptance checks")
    ok = True

    # 1. no null/empty ka_content
    n_null, st = scalar(
        f"SELECT count(*) FROM {T_TICKETS} "
        f"WHERE {KA_CONTENT_COL} IS NULL OR length({KA_CONTENT_COL}) = 0", profile)
    c1 = (st == "SUCCEEDED" and n_null is not None and int(n_null) == 0)
    ok &= c1
    print(f"  [{'PASS' if c1 else 'FAIL'}] no null/empty ka_content == 0 (got {n_null})")

    # 2. acronym expansion present for a system acronym that appears (AUR).
    n_aur, st = scalar(
        f"SELECT count(*) FROM {T_TICKETS} "
        f"WHERE {KA_CONTENT_COL} LIKE '%AUR (%'", profile)
    c2 = (st == "SUCCEEDED" and n_aur is not None and int(n_aur) > 0)
    ok &= c2
    print(f"  [{'PASS' if c2 else 'FAIL'}] AUR expansion present (ka_content LIKE "
          f"'%AUR (%') > 0 (got {n_aur})")

    # 3. no hardcoded acronym expansions in the source of THIS builder.
    src = Path(__file__).read_text()
    hardcoded = re.search(r"AUR\s*\(Automatic|WIM\s*\(Weigh", src)
    c3 = hardcoded is None
    ok &= c3
    print(f"  [{'PASS' if c3 else 'FAIL'}] no hardcoded acronym list in source "
          f"(grep clean = {c3})")

    print(f"[ka-content] verify: {'ALL PASS' if ok else 'FAIL'}")
    return ok



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
    ap = argparse.ArgumentParser(description="Build the KA content column (ENR-03).")
    # Accept --catalog/--schema/--warehouse-id so a DAB job task can retarget
    # this script. Serverless job environments cannot set env vars, so flags
    # are the only retargeting channel available to the bundle.
    _env.add_target_args(ap)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--verify", action="store_true",
                    help="run acceptance checks (implies a prior build)")
    args = ap.parse_args()
    # Rebind module globals BEFORE any table name is used.
    _apply_target(args)

    host = assert_target_host(args.profile)
    print(f"Host gate OK: {host}")

    build(args.profile)
    if args.verify:
        ok = verify(args.profile)
        sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
