"""Environment + auth configuration for the front-door app.

Dual-mode (mirrors app/server/config.py):
- Deployed as a Databricks App: uses the app service principal creds auto-injected
  by the platform.
- Local dev: uses the Databricks CLI profile (DATABRICKS_PROFILE or DEFAULT).

The MAS endpoint NAME is injected at deploy time via `valueFrom: serving-endpoint`
(06-05); the injected value is the endpoint name, not a URL.
"""
import os
from functools import lru_cache

# --- Host guard: this demo builds ONLY on the FIS l26d62 workspace (CLAUDE.md). ---
TARGET_HOST = "fevm-serverless-stable-l26d62.cloud.databricks.com"

# The warm Multi-Agent Supervisor endpoint (Phase 05) — reuse by NAME only, never
# re-create (that resets the per-tile SSP authorization). Injected via valueFrom in 06-05.
MAS_ENDPOINT_NAME = os.environ.get("MAS_ENDPOINT_NAME", "mas-f5fc28b0-endpoint")

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))


@lru_cache(maxsize=1)
def get_workspace_client():
    """Authenticated WorkspaceClient (dual-mode).

    - Deployed: app service principal credentials (auto-injected).
    - Local: Databricks CLI profile (DATABRICKS_PROFILE or DEFAULT).

    SDK import is lazy so the module stays importable in test/CI environments
    where databricks-sdk may not be installed but shape_answer is exercised.
    """
    from databricks.sdk import WorkspaceClient

    if IS_DATABRICKS_APP:
        return WorkspaceClient()
    profile = os.environ.get("DATABRICKS_PROFILE", "DEFAULT")
    return WorkspaceClient(profile=profile)


def get_host() -> str:
    """Workspace host (no scheme), asserted against the sanctioned FIS workspace."""
    host = get_workspace_client().config.host or ""
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    assert_target_host(host)
    return host


def assert_target_host(host: str) -> None:
    """Codebase convention: refuse any workspace call outside the FIS l26d62 env."""
    bare = host.replace("https://", "").replace("http://", "").rstrip("/")
    if bare != TARGET_HOST:
        raise RuntimeError(
            f"Refusing workspace call to '{bare}' — expected the FIS workspace "
            f"'{TARGET_HOST}' (CLAUDE.md platform constraint)."
        )
