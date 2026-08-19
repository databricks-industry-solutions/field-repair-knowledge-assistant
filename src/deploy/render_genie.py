#!/usr/bin/env python3
"""Render the Genie space payload for a target catalog/schema.

The native DAB `genie_spaces` resource (resources/genie.yml) uploads a static JSON
(`serialized_space`) and DAB does NOT interpolate variables inside it. So the space's
table references would otherwise be pinned to one catalog/schema and could not follow
a `--var schema=` override or a dev deployment.

This script substitutes `{{CATALOG}}` / `{{SCHEMA}}` in the tracked template
`genie/genie_space.template.json` and writes the rendered artifact
`genie/genie_space.json` (git-ignored) that `resources/genie.yml` points its
`file_path` at. Run it BEFORE `databricks bundle deploy`, with the SAME
catalog/schema you pass to the deploy:

    python3 src/deploy/render_genie.py --catalog <CATALOG> --schema <SCHEMA>

Pure stdlib, no workspace/REST call — safe to run offline and in CI.
"""

import argparse
import re
import sys
from pathlib import Path

import env as _env  # src/deploy is on sys.path[0] when run as a script

GENIE_DIR = Path(__file__).resolve().parents[2] / "genie"
TEMPLATE = GENIE_DIR / "genie_space.template.json"
RENDERED = GENIE_DIR / "genie_space.json"


def render(catalog, schema):
    if not TEMPLATE.exists():
        sys.exit(f"FATAL: template not found: {TEMPLATE}")
    text = TEMPLATE.read_text()
    text = text.replace("{{CATALOG}}", catalog).replace("{{SCHEMA}}", schema)
    left = re.findall(r"\{\{[A-Z_]+\}\}", text)
    if left:
        sys.exit(f"FATAL: unresolved placeholders remain: {sorted(set(left))}")
    RENDERED.write_text(text)
    print(f"Rendered {RENDERED.relative_to(GENIE_DIR.parent)} "
          f"for {catalog}.{schema} ({len(text)} bytes).")


def main():
    ap = argparse.ArgumentParser(description="Render the Genie space payload.")
    _env.add_target_args(ap)  # --catalog / --schema / --warehouse-id
    args = ap.parse_args()
    catalog = args.catalog or _env.CATALOG
    schema = args.schema or _env.SCHEMA
    if not catalog or not schema:
        sys.exit("FATAL: catalog/schema required (pass --catalog/--schema or set "
                 "FIS_CATALOG/FIS_SCHEMA).")
    render(catalog, schema)


if __name__ == "__main__":
    main()
