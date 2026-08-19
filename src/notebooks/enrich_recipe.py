"""Field Repair Knowledge Assistant — the SINGLE-SOURCE enrichment recipe.

Pure, dependency-free string/schema builders for the Lakeflow Declarative
Pipeline: the `ai_query` responseFormat + system prompt (gold_enrichment.py) and
the glossary-acronym expansion (serving.py). There is exactly ONE copy of the
ai_query schema, the system prompt, and the acronym expansion, so the two
pipeline datasets cannot drift apart.

Contract: this module must stay import-safe on Lakeflow serverless pipeline
compute, so it imports ONLY the stdlib (`json`, `re`) — never `preflight`,
`env`, `subprocess`, or anything that touches a warehouse/REST. All I/O (reading
the glossary) lives in the dataset files; the `parse_*` helpers here take
already-read rows (from `spark.read.table`).
"""

import json
import re

# --- enrichment model + provenance constants --------------------------------
# ai_query BATCH-capable endpoint. sonnet-5 is NOT batch-supported (Pitfall 2);
# sonnet-4-5 verified batch-capable.
CHAT_ENDPOINT = "databricks-claude-sonnet-4-5"
PROMPT_VERSION = "enrich-v3-desc-segmented"
CONF_THRESHOLD = 0.6  # min_confidence < 0.6 -> needs_review

# Fixed taxonomies (NOT glossary-coupled — these are not domain terms).
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

# The ENR-02 meaning-segmented columns that compose the KA content.
SEGMENT_COLS = ["summary", "troubleshooting", "root_cause", "resolution"]

# An acronym is a glossary term head matching this (mirrors normalize_text ACR).
ACR_RE = re.compile(r"^[A-Z0-9]{2,6}$")
SHORTDEF_MAX = 60


# --- glossary row parsers (GLO-02, pure — no I/O) ---------------------------

def parse_vocab(rows):
    """Build (systems, vendors, acr) from approved-glossary rows.

    `rows` is an iterable of (term, category, definition). NO hardcoded
    system/vendor list — a term is an enum value iff its glossary category says
    so (the GLO-02 coupling). `acr` maps an acronym term to a short definition
    used only as a normalization hint in the system prompt.
    """
    systems, vendors, acr = set(), set(), {}
    for term, category, definition in rows:
        head = (term or "").split(" / ")[0]
        if category == "system":
            systems.add(head)
        elif category == "vendor":
            vendors.add(head)
        if re.fullmatch(r"[A-Z0-9]{2,6}", head or ""):
            acr[term] = (definition or "").split(".")[0][:70]
    return sorted(systems), sorted(vendors), acr


def parse_acronym_map(rows):
    """{acronym: short_def} from approved-glossary rows of (term, definition).

    An acronym is expandable iff it is an approved glossary term head matching
    [A-Z0-9]{2,6}. Short def = first clause of the definition (mirrors the
    reference `normalize_text` derivation). NO hardcoded acronym list (GLO-02).
    """
    acr = {}
    for term, definition in rows:
        head = (term or "").split(" / ")[0]
        if not ACR_RE.match(head):
            continue
        short = (definition or "").split(".")[0].split(" — ")[0].split(" (")[0]
        short = re.sub(r"\s+", " ", short).strip()[:SHORTDEF_MAX].strip()
        if short:
            acr[head] = short
    return acr


# --- ai_query responseFormat + system prompt --------------------------------

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
        "You enrich roadside truck-screening R&D tickets (weigh-in-motion, "
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


# --- glossary-acronym expansion (KA content, ENR-03) ------------------------

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
    another acronym (Aptix->ALPR, CA->HTS, Falcon->ATIS), which produced
    nested garbage in the indexed content. The patterns are case-SENSITIVE, so
    lower-casing a cross-referenced token ("ALPR" -> "alpr") makes it unmatchable
    by every later link while KEEPING the word. Only affects DEFINITION text.
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
    of the (dotall) text through the first STANDALONE ACR token. Definitions are
    de-cascaded first so an inserted expansion cannot itself be expanded by a
    later link in the chain (see decascade).
    """
    expr = col
    for acr, short in decascade(acr_map).items():
        pattern = f"(?s)^(.*?)(^|[^A-Za-z0-9]){acr}([^A-Za-z0-9]|$)"
        repl = _sql_replacement(acr, short)
        expr = (f"regexp_replace({expr}, '{_sql_str(pattern)}', "
                f"'{_sql_str(repl)}')")
    return expr
