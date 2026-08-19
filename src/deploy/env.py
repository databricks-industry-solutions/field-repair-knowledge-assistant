"""Single source of truth for the deployment target (catalog / schema / warehouse).

Every build script used to hardcode:
    CATALOG      = "main"
    SCHEMA       = "rkb_knowledge_agent"
    WAREHOUSE_ID = "04a4dee7888b9e64"
across 17 files, which made the code un-targetable: a bundle could not point a
dev and a prod target at different catalogs, and a second workspace could not run
it at all.

Resolution order (first wins):
  1. environment variable  — RKB_CATALOG / RKB_SCHEMA / RKB_WAREHOUSE_ID
  2. the historical default — so every existing local invocation keeps working
     unchanged, with no flag and no env var.

The defaults are deliberate, not laziness: this is a demo pinned to one workspace
by the workspace policy, and silently resolving to an EMPTY catalog would turn a missing env
var into a confusing SQL error instead of a working run. A bundle sets the env
vars explicitly (see resources/jobs_*.yml).

Usage:
    from env import CATALOG, SCHEMA, WAREHOUSE_ID, FQ
    # or, for scripts that want an override flag:
    from env import add_target_args, apply_target_args
"""

import os

# --- defaults ---------------------------------------------------------------
# Neutral, so this template does not silently write into someone else's catalog.
# There is NO default warehouse: a wrong warehouse id fails confusingly, so it must
# be supplied explicitly via RKB_WAREHOUSE_ID or --warehouse-id.
DEFAULT_CATALOG = "main"
DEFAULT_SCHEMA = "troubleshooting_knowledge_agent"
DEFAULT_WAREHOUSE_ID = ""  # REQUIRED: set RKB_WAREHOUSE_ID or pass --warehouse-id
DEFAULT_PROFILE = "DEFAULT"

CATALOG = os.environ.get("RKB_CATALOG") or DEFAULT_CATALOG
SCHEMA = os.environ.get("RKB_SCHEMA") or DEFAULT_SCHEMA
WAREHOUSE_ID = os.environ.get("RKB_WAREHOUSE_ID") or DEFAULT_WAREHOUSE_ID
PROFILE = os.environ.get("RKB_PROFILE") or DEFAULT_PROFILE

FQ = f"{CATALOG}.{SCHEMA}"


def add_target_args(parser):
    """Add --catalog/--schema/--warehouse-id to an argparse parser.

    Defaults come from the env-resolved values, so the flags are a third override
    layer above env vars and built-in defaults.
    """
    parser.add_argument("--catalog", default=CATALOG,
                        help=f"UC catalog (default: {CATALOG})")
    parser.add_argument("--schema", default=SCHEMA,
                        help=f"UC schema (default: {SCHEMA})")
    parser.add_argument("--warehouse-id", default=WAREHOUSE_ID,
                        help=f"SQL warehouse id (default: {WAREHOUSE_ID})")
    return parser


def apply_target_args(args):
    """Rebind the module-level target from parsed args and return (catalog, schema, fq, wh).

    Scripts that import CATALOG/SCHEMA/FQ at module scope read them at import
    time, so this also updates this module's globals for anything importing lazily.
    """
    global CATALOG, SCHEMA, WAREHOUSE_ID, FQ
    CATALOG = getattr(args, "catalog", None) or CATALOG
    SCHEMA = getattr(args, "schema", None) or SCHEMA
    WAREHOUSE_ID = getattr(args, "warehouse_id", None) or WAREHOUSE_ID
    FQ = f"{CATALOG}.{SCHEMA}"
    return CATALOG, SCHEMA, FQ, WAREHOUSE_ID


def describe():
    """One-line summary of the resolved target, for build-script banners."""
    src = []
    if os.environ.get("RKB_CATALOG"):
        src.append("catalog=env")
    if os.environ.get("RKB_SCHEMA"):
        src.append("schema=env")
    if os.environ.get("RKB_WAREHOUSE_ID"):
        src.append("warehouse=env")
    origin = f" ({', '.join(src)})" if src else " (defaults)"
    return f"target: {FQ} on warehouse {WAREHOUSE_ID}{origin}"
