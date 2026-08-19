#!/usr/bin/env python3
"""
FIS AI Knowledge Agent — Phase 5 Plan 02: Multi-Agent Supervisor ROUTING harness.

Re-runnable assertion harness that proves the DEPLOYED 3-tool Multi-Agent
Supervisor (`fis-rnd-supervisor`, endpoint `mas-f5fc28b0-endpoint`, built in Plan
05-01) routes each of the 5 `Prompts.md` archetypes (plus 1-2 paraphrases each) to
the correct tool(s), fans out to BOTH KA and Genie on the three hybrid archetypes
(A2b expert-finding, A3 complexity/delay, A4 priority triage), and produces the
required answer STRUCTURE for the two marquee archetypes (A4 three buckets with
ticket pointers; A5 named tickets + software/hardware split + prescribed
direction).

Verification is content-anchored, NOT prose-trusting:

  * PRIMARY (trace-equivalent): the MAS Responses-API body returns the tool spans
    inline — `output[]` carries `type=="function_call"` / `"function_call_output"`
    items whose `name` is the FULLY-QUALIFIED tool name (dunder-separated, e.g.
    `serverless_stable_l26d62_catalog__fis_knowledge_agent__glossary_lookup`,
    `ka-97df484b-...`, `genie-01f185f0...`). We derive the fired-set from these
    spans via SUBSTRING matching (never exact equality — the names come back
    fully-qualified/dunder-separated per the 05-01 carry-forward).
  * FALLBACK (CONTEXT-approved, Pitfall 4): three content signals — ticket
    citations => KA fired; numeric aggregates / ranked ticket rows => Genie fired;
    a returned term+definition+category => glossary_lookup fired. Recorded per row
    as the evidence source when spans are unavailable.

Design (mirrors src/deploy/test_genie.py + test_ka.py — the repo convention, NOT
pytest, which is not installed):
  - Step 0 host-assertion gate (reuse preflight.assert_target_host) — refuse any
    workspace but l26d62 (T-5-01).
  - Pre-matrix three-way GRANT GATE (SUP-01, T-5-03): re-assert EXECUTE on
    glossary_lookup + SELECT on rd_tasks_gold_analytics + KA endpoint READY, so a
    routing FAIL is never masked by a missing grant.
  - Structured verdicts {criterion, status, evidence}; exit non-zero on any FAIL.
  - CONCURRENT invocation (05-01 optimization): the warm endpoint is fired over a
    thread pool so the ~10-12 archetype round-trips overlap, not serialize.
  - --only <SUP-id[,SUP-id]> filter; writes 05-SUPERVISOR-ROUTING.md.

Usage:
    python3 src/deploy/test_supervisor.py --profile serverless-stable
    python3 src/deploy/test_supervisor.py --profile serverless-stable --only SUP-02,SUP-03
    python3 src/deploy/test_supervisor.py --profile serverless-stable --only SUP-04
"""

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Reuse the Phase-1 host-safety gate + SQL helper ------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "preflight"))
from preflight import assert_target_host, run_sql, resolve_principal  # noqa: E402

# --- deployment target: catalog/schema/warehouse (see preflight/env.py) ---
# Centralised so a DAB target can retarget this without editing 14 files.
# Defaults to the historical l26d62 values, so local runs are unchanged.
import env as _env  # noqa: E402  (preflight/ already on sys.path above)

WAREHOUSE_ID = _env.WAREHOUSE_ID
CATALOG = _env.CATALOG
SCHEMA = _env.SCHEMA
GLOSSARY_FN = f"{CATALOG}.{SCHEMA}.glossary_lookup"
ANALYTICS_VIEW = f"{CATALOG}.{SCHEMA}.rd_tasks_gold_analytics"

# The live tool identifiers (05-SUPERVISOR-BUILD.md). Substring-matched in the
# dunder-qualified tool-call names the MAS returns (NEVER exact equality).
KA_TILE_FRAG = "97df484b"
GENIE_SPACE_FRAG = "01f185f0ce8e15cd9a92d86b3171c52e"
KA_ENDPOINT = "ka-97df484b-endpoint"

BUILD_DOC = (
    REPO_ROOT
    / ".planning/phases/05-multi-agent-supervisor/05-SUPERVISOR-BUILD.md"
)
REPORT_PATH = (
    REPO_ROOT
    / ".planning/phases/05-multi-agent-supervisor/05-SUPERVISOR-ROUTING.md"
)

# Ticket-number pattern (corpus uses R&DTASK<7 digits>; tolerate the &-dropped form).
TICKET_RE = re.compile(r"R&?DTASK\d{7}")

# Concurrency ceiling — the warm endpoint is fired in parallel (05-01 optimization).
# Kept modest: the heaviest hybrid fan-out queries (glossary+KA+Genie in one turn)
# are long-running, and too much concurrent pressure makes the serving gateway
# cancel queued requests ("context canceled"). 3 overlaps the round-trips while
# staying under the endpoint's concurrent-request pressure point.
MAX_WORKERS = 3
INVOKE_TIMEOUT_S = 300
# Transient transport errors (gateway cancels a queued long request) are retried
# with backoff — distinct from a genuine routing/answer failure.
INVOKE_RETRIES = 3
RETRY_BACKOFF_S = 8
_TRANSIENT_MARKERS = ("context canceled", "context deadline", "timeout",
                      "EOF", "connection reset", "502", "503", "504")


def verdict(criterion, status, evidence, source=""):
    return {"criterion": criterion, "status": status,
            "evidence": evidence, "source": source}


# --- CLI + endpoint discovery -----------------------------------------------

def run_cli(args, profile, timeout=180):
    cmd = ["databricks", *args, "--profile", profile]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "databricks CLI not found"


def resolve_host_token(profile):
    """Resolve the workspace host + a fresh OAuth access token for the profile.

    The MAS invocation is issued with `curl` (not `databricks api`) because the
    CLI's HTTP client cancels any request exceeding ~60s ("context canceled"),
    and the heaviest hybrid fan-out queries (glossary+KA+Genie in one turn) run
    90-150s. curl lets us set a generous --max-time. Returns (host, token)."""
    code, out, _ = run_cli(["auth", "token"], profile)
    token = ""
    if code == 0:
        try:
            token = json.loads(out).get("access_token", "")
        except json.JSONDecodeError:
            token = ""
    code, out, _ = run_cli(["auth", "env"], profile)
    host = ""
    if code == 0:
        try:
            host = json.loads(out).get("env", {}).get("DATABRICKS_HOST", "")
        except json.JSONDecodeError:
            host = ""
    return host.rstrip("/"), token


def read_mas_endpoint():
    """Parse the MAS serving-endpoint name from 05-SUPERVISOR-BUILD.md (single
    source of truth — do NOT hardcode; the endpoint name is discovered at build
    time)."""
    if not BUILD_DOC.exists():
        return None
    txt = BUILD_DOC.read_text()
    m = re.search(r"serving endpoint\s*\|\s*`([a-zA-Z0-9\-]+)`", txt)
    if m:
        return m.group(1)
    m = re.search(r"(mas-[0-9a-f]+-endpoint)", txt)
    return m.group(1) if m else None


def endpoint_ready(profile, endpoint):
    """Assert the warm endpoint is READY and reuse it as-is (05-01 optimization:
    do NOT re-provision; a re-create would reset the manually-authorized SSP)."""
    code, out, _ = run_cli(
        ["serving-endpoints", "get", endpoint, "-o", "json"], profile)
    if code != 0:
        return False, "not found", None
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        return False, "unparseable", None
    state = (d.get("state") or {})
    ready = str(state.get("ready", "")).upper() == "READY"
    return ready, json.dumps(state), d.get("task")


# --- MAS invocation (Responses-API + inline trace) --------------------------

def _is_transient(msg):
    low = (msg or "").lower()
    return any(m.lower() in low for m in _TRANSIENT_MARKERS)


def invoke_mas(endpoint, question, profile, host="", token=""):
    """POST the Responses-API {"input":[...]} shape with return_trace=true, via
    curl (NOT `databricks api`, whose ~60s client timeout cancels long fan-out
    queries). Returns (ok, resp_dict, err). NEVER {"messages":...} (Pitfall 5).

    Retries transient transport errors with backoff, so a transport blip is
    never misreported as a routing FAIL."""
    import time
    body = json.dumps({
        "input": [{"role": "user", "content": question}],
        "databricks_options": {"return_trace": True},
    })
    url = f"{host}/serving-endpoints/{endpoint}/invocations"
    last_err = ""
    for attempt in range(1, INVOKE_RETRIES + 1):
        try:
            p = subprocess.run(
                ["curl", "-sS", "--max-time", str(INVOKE_TIMEOUT_S),
                 "-X", "POST", url,
                 "-H", f"Authorization: Bearer {token}",
                 "-H", "Content-Type: application/json",
                 "-d", body, "-w", "\n%{http_code}"],
                capture_output=True, text=True, timeout=INVOKE_TIMEOUT_S + 30)
        except subprocess.TimeoutExpired:
            last_err = "curl timeout"
            if attempt < INVOKE_RETRIES:
                time.sleep(RETRY_BACKOFF_S * attempt)
                continue
            break
        raw = p.stdout
        http_code = raw.rsplit("\n", 1)[-1].strip() if "\n" in raw else ""
        payload = raw.rsplit("\n", 1)[0] if "\n" in raw else raw
        if p.returncode == 0 and http_code == "200":
            try:
                return True, json.loads(payload), ""
            except json.JSONDecodeError:
                last_err = f"non-JSON response: {payload[:200]}"
                return False, None, last_err
        last_err = (f"curl rc={p.returncode} http={http_code}: "
                    f"{(p.stderr or payload)[:200]}")
        if attempt < INVOKE_RETRIES and (
                _is_transient(p.stderr or payload) or http_code in ("", "502",
                                                                    "503", "504")):
            time.sleep(RETRY_BACKOFF_S * attempt)
            continue
        break
    return False, None, last_err


def prose_of(resp):
    """Final answer text = concat of assistant message output[].content[].text."""
    return "".join(
        c.get("text", "")
        for o in (resp.get("output") or [])
        for c in (o.get("content") or [])
        if isinstance(c, dict) and "text" in c
    )


# --- Fired-set derivation: trace spans first, content signals fallback ------

def _classify_tool(name):
    """Map a (dunder-qualified) tool-call name to KA | Genie | glossary via
    SUBSTRING match (never exact — names come back fully-qualified, 05-01)."""
    n = (name or "").lower()
    if "glossary_lookup" in n:
        return "glossary"
    if KA_TILE_FRAG in n or "knowledge" in n or n.startswith("ka-"):
        return "KA"
    if "genie" in n or GENIE_SPACE_FRAG in n:
        return "Genie"
    return None


def fired_from_spans(resp):
    """PRIMARY path: derive fired tools from the inline function_call spans in
    output[]. Returns (fired_set, raw_tool_names)."""
    fired, names = set(), []
    for o in resp.get("output") or []:
        if o.get("type") in ("function_call", "function_call_output"):
            nm = o.get("name")
            if nm:
                names.append(nm)
                t = _classify_tool(nm)
                if t:
                    fired.add(t)
    return fired, names


def fired_from_content(resp):
    """FALLBACK path (Pitfall 4, CONTEXT-approved three content signals):
      ticket citations           => KA
      numeric aggregate/rank rows => Genie
      term+definition+category    => glossary_lookup
    Returns a fired_set inferred purely from answer content."""
    fired = set()
    prose = prose_of(resp)
    low = prose.lower()
    # KA: ticket citations present in the merged answer.
    if TICKET_RE.search(prose):
        fired.add("KA")
    # glossary: a returned term + definition + category signal.
    if "category" in low and ("glossary" in low or "definition" in low
                              or "stands for" in low or "software term" in low):
        fired.add("glossary")
    # Genie: numeric aggregates / ranked rows / count language.
    if re.search(r"\b\d+\s+(open|pending|tasks?|cases?)\b", low) or \
       re.search(r"\b(count|ranked|top \d|number of|total of)\b", low):
        fired.add("Genie")
    return fired


def derive_fired(resp):
    """Return (fired_set, source_label, raw_names). Trace spans are authoritative
    when present; otherwise fall back to content signals."""
    spans, names = fired_from_spans(resp)
    if spans:
        return spans, "function_call span (trace)", names
    return fired_from_content(resp), "content signal", names


# --- Ground-truth corpus cross-check ----------------------------------------

def _norm_ticket(t):
    t = t.upper()
    if t.startswith("RDTASK"):
        t = t.replace("RDTASK", "R&DTASK")
    return t


def tickets_in_corpus(numbers, profile):
    """Return the subset of ticket numbers that actually exist in the live
    analytics view (grounding pointers — not hallucinated)."""
    nums = sorted({_norm_ticket(n) for n in numbers})
    if not nums:
        return set()
    in_list = ",".join("'" + n.replace("'", "''") + "'" for n in nums)
    state, rows = run_sql(
        f"SELECT task_number FROM {ANALYTICS_VIEW} WHERE task_number IN ({in_list})",
        profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not rows:
        return set()
    return {str(r[0]) for r in rows}


# --- SUP-01: pre-matrix three-way grant gate --------------------------------

def grant_gate(profile, principal):
    """Re-assert the three tool-access surfaces BEFORE the matrix so a routing
    FAIL is never masked by a missing grant (Pitfall 2 / T-5-03). Returns
    (ok, evidence_lines). The per-tile MAS SSP grant is UI-authorized and not
    visible to workspace SQL (05-01) — its liveness is proven downstream by the
    tools actually executing in the matrix; here we assert every SQL-visible
    surface for the demo principal."""
    ev = []
    ok = True

    # 1. EXECUTE on glossary_lookup is present.
    state, rows = run_sql(f"SHOW GRANTS ON FUNCTION {GLOSSARY_FN}", profile,
                          WAREHOUSE_ID)
    flat = json.dumps(rows or [])
    exec_present = state == "SUCCEEDED" and ("EXECUTE" in flat
                                             or "ALL PRIVILEGES" in flat)
    ev.append(f"EXECUTE on glossary_lookup present={exec_present} "
              f"(SHOW GRANTS state={state})")
    ok = ok and exec_present

    # 2. SELECT on the Genie backing view succeeds.
    state, rows = run_sql(f"SELECT count(*) FROM {ANALYTICS_VIEW}", profile,
                          WAREHOUSE_ID)
    sel_ok = state == "SUCCEEDED"
    ev.append(f"SELECT on {ANALYTICS_VIEW}: {state} "
              f"(rows={rows[0][0] if sel_ok and rows else '?'})")
    ok = ok and sel_ok

    # 3. KA endpoint is READY (CAN QUERY reachable).
    ka_ready, ka_state, _ = endpoint_ready(profile, KA_ENDPOINT)
    ev.append(f"KA endpoint {KA_ENDPOINT} READY={ka_ready} ({ka_state})")
    ok = ok and ka_ready

    ev.append(f"demo principal={principal}; per-tile MAS SSP tool access is "
              "UI-authorized (05-01) and proven live by tool execution in the matrix")
    return ok, ev


# --- Routing matrix ----------------------------------------------------------
# Each row: id, archetype, question (leakage-free — grounded on the live synthetic
# corpus, NOT Prompts.md verbatim, no real-sample ticket numbers), expected fired
# tools, whether fan-out (BOTH KA+Genie) is required, and the SUP ids it feeds.
# glossary is accepted as an ADDITIONAL hop on acronym-bearing rows; REQUIRED on A1.

MATRIX = [
    # A1 — terminology: glossary REQUIRED.
    {"id": "A1", "sup": {"SUP-02"},
     "q": "What does the acronym CA mean in these R&D tickets, and is it a "
          "roadside screening system or a software/controller term?",
     "expect": {"glossary"}, "require_glossary": True, "fanout": False},
    {"id": "A1b", "sup": {"SUP-02"},
     "q": "Explain what HTS refers to as it is used across our R&D tickets.",
     "expect": {"glossary"}, "require_glossary": True, "fanout": False},

    # A2a — counts: Genie.
    {"id": "A2a", "sup": {"SUP-02"},
     "q": "How many tasks are currently open or pending in New Mexico?",
     "expect": {"Genie"}, "require_glossary": False, "fanout": False},
    {"id": "A2a2", "sup": {"SUP-02"},
     "q": "Count how many open tasks are located in Virginia.",
     "expect": {"Genie"}, "require_glossary": False, "fanout": False},

    # A2b — expert-finding: KA + Genie fan-out.
    {"id": "A2b", "sup": {"SUP-02", "SUP-03"},
     "q": "Who is the go-to engineer for AUR camera issues, and which prior "
          "cases back that up?",
     "expect": {"KA", "Genie"}, "require_glossary": False, "fanout": True},
    {"id": "A2b2", "sup": {"SUP-02", "SUP-03"},
     "q": "Which engineer resolves the most WIM weight problems? Cite the "
          "prior cases that support it.",
     "expect": {"KA", "Genie"}, "require_glossary": False, "fanout": True},

    # A3 — complexity/delay: KA + Genie fan-out.
    {"id": "A3", "sup": {"SUP-02", "SUP-03"},
     "q": "Which kinds of tasks take the longest to resolve, and why?",
     "expect": {"KA", "Genie"}, "require_glossary": False, "fanout": True},
    {"id": "A3b", "sup": {"SUP-02", "SUP-03"},
     "q": "What categories of tasks are the slowest to close, and what is "
          "driving the delay?",
     "expect": {"KA", "Genie"}, "require_glossary": False, "fanout": True},

    # A4 — priority triage (marquee): KA + Genie fan-out. Content asserted in SUP-04.
    {"id": "A4", "sup": {"SUP-02", "SUP-03", "SUP-04"},
     "q": "Among our currently open tasks, which should the team prioritize "
          "right now, and why?",
     "expect": {"KA", "Genie"}, "require_glossary": False, "fanout": True},

    # A5 — site recurring patterns: KA-primary (+ Genie grouping). Content in SUP-05.
    {"id": "A5", "sup": {"SUP-02", "SUP-05"},
     "q": "What recurring problems keep coming back in New Mexico, and what "
          "should we do about them?",
     "expect": {"KA"}, "require_glossary": False, "fanout": False},
]


def rows_for(only):
    """Which matrix rows to fire, given the --only SUP-id filter."""
    if not only:
        return MATRIX
    return [r for r in MATRIX if r["sup"] & only]


def fire_matrix(endpoint, rows, profile, host="", token=""):
    """Invoke every selected row CONCURRENTLY against the warm endpoint (05-01
    optimization). Returns {id: {row, resp, fired, source, names, prose, err}}."""
    results = {}

    def _one(row):
        ok, resp, err = invoke_mas(endpoint, row["q"], profile, host, token)
        if not ok:
            return row["id"], {"row": row, "resp": None, "fired": set(),
                               "source": "error", "names": [], "prose": "",
                               "err": err}
        fired, source, names = derive_fired(resp)
        return row["id"], {"row": row, "resp": resp, "fired": fired,
                           "source": source, "names": names,
                           "prose": prose_of(resp), "err": ""}

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for rid, rec in ex.map(_one, rows):
            results[rid] = rec
    return results


# --- SUP-02: routing correctness ---------------------------------------------

def check_sup02(results):
    """Every row's observed fired-set must CONTAIN its expected tool(s); A1 must
    include the glossary hop. glossary is accepted as an extra hop elsewhere."""
    vs = []
    for rid, rec in results.items():
        row = rec["row"]
        if "SUP-02" not in row["sup"]:
            continue
        fired = rec["fired"]
        if rec["resp"] is None:
            vs.append(verdict(f"SUP-02 [{rid}] routes to {sorted(row['expect'])}",
                              "FAIL", f"invocation failed: {rec['err']}",
                              rec["source"]))
            continue
        missing = row["expect"] - fired
        glossary_ok = (not row["require_glossary"]) or ("glossary" in fired)
        passed = not missing and glossary_ok
        vs.append(verdict(
            f"SUP-02 [{rid}] routes to {sorted(row['expect'])}"
            + (" (+glossary required)" if row["require_glossary"] else ""),
            "PASS" if passed else "FAIL",
            f"observed={sorted(fired) or 'none'}; missing={sorted(missing) or 'none'}; "
            f"glossary_hop={'glossary' in fired}; tool spans={rec['names']}",
            rec["source"]))
    return vs


# --- SUP-03: fan-out to BOTH KA and Genie on the hybrids ---------------------

def check_sup03(results):
    """A2b/A3/A4 must each show BOTH KA and Genie in the fired-set (never a
    silent single-signal pass — a single-engine hybrid is a FAIL / known
    limitation, not a pass)."""
    vs = []
    for rid, rec in results.items():
        row = rec["row"]
        if "SUP-03" not in row["sup"] or not row["fanout"]:
            continue
        fired = rec["fired"]
        if rec["resp"] is None:
            vs.append(verdict(f"SUP-03 [{rid}] fans out to BOTH KA + Genie",
                              "FAIL", f"invocation failed: {rec['err']}",
                              rec["source"]))
            continue
        both = {"KA", "Genie"} <= fired
        vs.append(verdict(
            f"SUP-03 [{rid}] fans out to BOTH KA + Genie",
            "PASS" if both else "FAIL",
            f"observed={sorted(fired) or 'none'}; both KA+Genie present={both}; "
            f"tool spans={rec['names']}"
            + ("" if both else " (known-limitation if single-engine persists — "
               "Pitfall 3 / Assumption A4)"),
            rec["source"]))
    return vs


# --- SUP-04: A4 three-bucket triage + corpus-grounded ticket pointers -------

# Bucket concepts (SHAPE, not Prompts.md verbatim). Each bucket is detected by a
# concept-anchor; a ticket-number pointer must appear in the window after it.
A4_BUCKETS = [
    ("easiest-with-known-fix",
     r"(easiest|quick win|quick resolution|known fix|solved twin|solved "
     r"similar|has a solved)"),
    ("oldest-but-blocked",
     r"(oldest|longest[- ]pending|longest open|blocked|dependency|"
     r"procurement|awaiting|stalled)"),
    ("stubborn-recurring",
     r"(stubborn|recurring|repeatedly|repeated|keeps? (coming|failing)|"
     r"prior fixes? fail|revisited)"),
]
BUCKET_WINDOW = 900  # chars after an anchor to look for a ticket pointer


def _bucket_pointer(prose, pattern):
    """Return (found_anchor, ticket_number_or_None) for one bucket pattern."""
    m = re.search(pattern, prose, re.IGNORECASE)
    if not m:
        return False, None
    window = prose[m.start(): m.start() + BUCKET_WINDOW]
    tm = TICKET_RE.search(window)
    return True, (tm.group(0) if tm else None)


def check_sup04(rec, profile):
    """A4 answer must surface all THREE buckets (easiest-with-known-fix /
    oldest-but-blocked / stubborn-recurring), each with >=1 concrete ticket
    pointer, and those pointers must exist in the live corpus (not hallucinated).
    Assert on SHAPE, never on Prompts.md ticket numbers."""
    if rec is None or rec["resp"] is None:
        return verdict("SUP-04 A4 three-bucket triage + corpus-grounded pointers",
                       "FAIL",
                       f"A4 invocation failed: {rec['err'] if rec else 'not fired'}",
                       "error")
    prose = rec["prose"]
    bucket_hits, bucket_tickets = [], []
    for label, pat in A4_BUCKETS:
        found, tk = _bucket_pointer(prose, pat)
        bucket_hits.append((label, found, tk))
        if tk:
            bucket_tickets.append(tk)
    all_buckets = all(f for _, f, _ in bucket_hits)
    all_have_ticket = all(tk for _, _, tk in bucket_hits)
    resident = tickets_in_corpus(bucket_tickets, profile)
    pointers_real = bool(bucket_tickets) and all(
        _norm_ticket(t) in resident for t in bucket_tickets)
    passed = all_buckets and all_have_ticket and pointers_real
    detail = "; ".join(
        f"{label}: anchor={f}, ticket={tk}"
        + ("" if not tk else f" ({'∈corpus' if _norm_ticket(tk) in resident else 'NOT in corpus'})")
        for label, f, tk in bucket_hits)
    return verdict(
        "SUP-04 A4 three-bucket triage + corpus-grounded pointers",
        "PASS" if passed else "FAIL",
        f"{detail}; all 3 buckets present={all_buckets}; each has a ticket="
        f"{all_have_ticket}; pointers ∈corpus={pointers_real}",
        rec["source"])


# --- SUP-05: A5 named tickets + software/hardware split + prescribed direction

def check_sup05(rec, profile):
    """A5 answer must (1) name >=2 corpus-resident ticket numbers, (2) explicitly
    separate software-root-cause from hardware-root-cause, and (3) prescribe a
    direction. Assert on SHAPE, grounded on the live corpus."""
    if rec is None or rec["resp"] is None:
        return verdict("SUP-05 A5 named tickets + sw/hw split + direction",
                       "FAIL",
                       f"A5 invocation failed: {rec['err'] if rec else 'not fired'}",
                       "error")
    prose = rec["prose"]
    low = prose.lower()
    cited = sorted(set(TICKET_RE.findall(prose)))
    resident = tickets_in_corpus(cited, profile)
    n_real = len(resident)
    named_ok = n_real >= 2
    # sw/hw split: both a software-root-cause and a hardware-root-cause signal.
    sw = bool(re.search(r"software(?![- ]?crash\|)|memory leak|firmware|"
                        r"race condition|build \d|heap|connection pool", low))
    hw = bool(re.search(r"hardware|enclosure|power supply|psu|connector|"
                        r"cabling|sensor (drift|misalign)|material|corro", low))
    # require the answer to explicitly frame BOTH as (root) causes.
    frames_cause = ("root cause" in low or "root-cause" in low
                    or "causes" in low or "cause:" in low)
    split_ok = sw and hw and frames_cause
    # prescribes a direction.
    direction_ok = bool(re.search(
        r"recommend|recommended action|should|deploy|escalate|next step|"
        r"action:|prioriti|replace|schedule|evaluate", low))
    passed = named_ok and split_ok and direction_ok
    return verdict(
        "SUP-05 A5 named tickets + sw/hw split + direction",
        "PASS" if passed else "FAIL",
        f"named corpus tickets={sorted(resident)} (>=2 required, got {n_real}); "
        f"software-cause signal={sw}; hardware-cause signal={hw}; "
        f"frames as (root) cause={frames_cause}; split_ok={split_ok}; "
        f"prescribes direction={direction_ok}",
        rec["source"])


# --- report ------------------------------------------------------------------

def write_report(host, endpoint, results, verdicts, extra_sections=None):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_pass = sum(1 for v in verdicts if v["status"] == "PASS")
    n_fail = len(verdicts) - n_pass
    lines = [
        "# 05-SUPERVISOR-ROUTING — Multi-Agent Supervisor Routing Evidence (Plan 05-02)",
        "",
        f"**Generated:** {ts}",
        f"**Workspace:** `{host}`",
        f"**MAS endpoint:** `{endpoint}` (reused warm — READY; NOT re-provisioned, "
        "per 05-01 carry-forward)",
        f"**Harness:** `src/deploy/test_supervisor.py` (re-runnable; exits non-zero on any FAIL)",
        "",
        "Routing is verified content-anchored, not prose-trusting. **Primary "
        "(trace-equivalent):** the MAS Responses-API body returns the fired tools "
        "inline as `output[].type == function_call` items whose `name` is the "
        "dunder-qualified tool name (`...__glossary_lookup`, `ka-97df484b-...`, "
        "`genie-01f185f0...`) — matched by SUBSTRING (never exact equality). "
        "**Fallback (CONTEXT-approved, Pitfall 4):** ticket citations => KA; "
        "numeric aggregates/ranked rows => Genie; term+definition+category => "
        "glossary_lookup. The `Evidence source` column records which carried each row.",
        "",
        f"**Result: {n_pass} PASS / {n_fail} FAIL of {len(verdicts)} assertions.**",
        "",
        "## Per-Archetype Routing Matrix",
        "",
        "| Archetype | Question | Expected fired-set | Observed fired-set | "
        "Verdict | Evidence source |",
        "|-----------|----------|--------------------|--------------------|"
        "---------|-----------------|",
    ]
    for rid in [r["id"] for r in MATRIX]:
        rec = results.get(rid)
        if not rec:
            continue
        row = rec["row"]
        exp = sorted(row["expect"]) + (["glossary*"] if row["require_glossary"]
                                       and "glossary" not in row["expect"] else [])
        obs = sorted(rec["fired"]) or ["none"]
        # per-row verdict = fan-out (if hybrid) else routing
        if row["fanout"]:
            ok = {"KA", "Genie"} <= rec["fired"]
        else:
            ok = row["expect"] <= rec["fired"] and (
                not row["require_glossary"] or "glossary" in rec["fired"])
        q = row["q"].replace("|", "\\|")
        lines.append(
            f"| {rid} | {q} | {', '.join(exp)} | {', '.join(obs)} | "
            f"{'PASS' if ok else 'FAIL'} | {rec['source']} |")
    lines.append("")
    lines.append("## Assertion Verdicts")
    lines.append("")
    lines.append("| Status | Criterion | Evidence | Source |")
    lines.append("|--------|-----------|----------|--------|")
    for v in verdicts:
        ev = v["evidence"].replace("|", "\\|")
        lines.append(f"| {v['status']} | {v['criterion']} | {ev} | {v['source']} |")
    lines.append("")
    for section in (extra_sections or []):
        lines.append(section)
        lines.append("")
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines))
    print(f"Wrote {REPORT_PATH}")


def build_human_verify_sections(a4, a5):
    """Two clearly-labelled human-verify notes (05-VALIDATION Manual-Only
    Verifications) + A4/A5 answer excerpts for the fan-out eyeball."""
    def _excerpt(rec, n=1400):
        if not rec or not rec.get("prose"):
            return "_(answer unavailable this run)_"
        return "```\n" + rec["prose"][-n:].strip() + "\n```"

    a4_spans = a4["names"] if a4 else []
    a4_both = a4 and {"KA", "Genie"} <= a4["fired"]
    sections = [
        "## Human-Verify Annotations (Manual-Only Verifications)",
        "",
        "> yolo/auto-advance is on, so these are surfaced as manual-check "
        "annotations rather than blocking checkpoints (05-VALIDATION.md).",
        "",
        "### HV-1 — Fan-out eyeball (SUP-03)",
        "",
        "The harness derives fan-out from AUTHORITATIVE inline `function_call` "
        "trace spans (not content alone), so both engines are proven "
        "programmatically. As a belt-and-suspenders manual check, eyeball the A4 "
        "answer below and confirm it carries BOTH a KA-style cited ticket "
        "(similar-case reasoning) AND a Genie-style ranked/aggregated open-task "
        "list. "
        + (f"A4 tool spans this run: `{a4_spans}` — both KA+Genie present="
           f"{bool(a4_both)}." if a4 else "A4 not fired this run."),
        "",
        "**A4 answer excerpt (tail):**",
        "",
        _excerpt(a4),
        "",
        "### HV-2 — Supervisor-LLM pin finding (SUP-01, Assumption A1)",
        "",
        "Recorded from the 05-01 build spike: **no supervisor-LLM/model field is "
        "exposed by `POST /api/2.1/supervisor-agents`** (nor by `manage_mas`). The "
        "supervisor LLM is a platform default — the "
        "`databricks-claude-sonnet-4-5` pin is NOT settable via this API. This is "
        "a platform constraint, not a build failure (see 05-SUPERVISOR-BUILD.md "
        "'Supervisor-LLM Pin Finding'). No action required; noted so the demo "
        "narrative does not claim a settable model.",
        "",
        "**A5 answer excerpt (tail) — sw/hw split eyeball:**",
        "",
        _excerpt(a5),
    ]
    return ["\n".join(sections)]


def print_table(verdicts):
    print(f"\n{'STATUS':6}  CRITERION")
    print("-" * 72)
    for v in verdicts:
        print(f"{v['status']:6}  {v['criterion']}")
        print(f"          └─ {v['evidence']}")
        print(f"          └─ evidence source: {v['source']}")
    n_pass = sum(1 for v in verdicts if v["status"] == "PASS")
    n_fail = len(verdicts) - n_pass
    print("-" * 72)
    print(f"{n_pass} PASS / {n_fail} FAIL of {len(verdicts)} assertions")
    return n_fail


# --- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="MAS routing/fan-out assertion harness")
    ap.add_argument("--profile", default="serverless-stable")
    ap.add_argument("--only", default="",
                    help="comma-separated SUP ids, e.g. SUP-02,SUP-03")
    args = ap.parse_args()

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}

    # Step 0 — never query the wrong/unauthenticated workspace (T-5-01).
    host = assert_target_host(args.profile)
    principal = resolve_principal(args.profile)
    print(f"Host gate OK: {host}")
    print(f"Demo principal: {principal}")

    endpoint = read_mas_endpoint()
    if not endpoint:
        print(f"FATAL: could not read MAS endpoint from {BUILD_DOC}", file=sys.stderr)
        sys.exit(2)
    ready, state, task = endpoint_ready(args.profile, endpoint)
    if not ready:
        print(f"FATAL: MAS endpoint {endpoint} not READY (state={state}). "
              "Do NOT re-provision — see 05-01 carry-forward.", file=sys.stderr)
        sys.exit(3)
    print(f"MAS endpoint {endpoint} READY (task={task}) — reusing warm.")

    host_url, token = resolve_host_token(args.profile)
    if not host_url or not token:
        print("FATAL: could not resolve host/OAuth token for the MAS invocation.",
              file=sys.stderr)
        sys.exit(5)

    # Pre-matrix grant gate (SUP-01, T-5-03) — fail fast on a grant gap.
    gate_ok, gate_ev = grant_gate(args.profile, principal)
    print("\nGrant gate (SUP-01 — three tool surfaces):")
    for e in gate_ev:
        print(f"  {e}")
    if not gate_ok:
        print("FATAL: grant gate FAILED — a routing FAIL would be masked by a "
              "missing grant. Fix grants before the matrix.", file=sys.stderr)
        sys.exit(4)

    # Fire the routing matrix concurrently against the warm endpoint.
    rows = rows_for(only)
    print(f"\nFiring {len(rows)} archetype question(s) concurrently "
          f"(max_workers={MAX_WORKERS})...")
    results = fire_matrix(endpoint, rows, args.profile, host_url, token)

    verdicts = []
    if not only or "SUP-02" in only:
        verdicts += check_sup02(results)
    if not only or "SUP-03" in only:
        verdicts += check_sup03(results)
    if not only or "SUP-04" in only:
        verdicts.append(check_sup04(results.get("A4"), args.profile))
    if not only or "SUP-05" in only:
        verdicts.append(check_sup05(results.get("A5"), args.profile))

    n_fail = print_table(verdicts)

    # Human-verify annotations (yolo/auto-advance is on — surface as manual-check
    # notes per 05-VALIDATION Manual-Only Verifications) + A4/A5 answer excerpts.
    a4, a5 = results.get("A4"), results.get("A5")
    extra = build_human_verify_sections(a4, a5)

    # Write the routing evidence table whenever the routing matrix ran (full run
    # only — a --only subset would clobber the table with a partial view).
    if verdicts and not only:
        write_report(host, endpoint, results, verdicts, extra)
    elif verdicts:
        print("(--only run: skipped rewriting 05-SUPERVISOR-ROUTING.md to avoid "
              "a partial-view clobber)")

    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
