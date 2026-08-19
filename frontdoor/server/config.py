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

# --- Optional host guard. Set RKB_TARGET_HOST to a host fragment to pin the app to
# one workspace; unset (the default) disables the guard so the template is portable. ---
TARGET_HOST = os.environ.get("RKB_TARGET_HOST", "")

# The warm Multi-Agent Supervisor endpoint — reuse by NAME only, never re-create
# (that resets the per-tile SSP authorization). Injected via valueFrom in the app resource.
MAS_ENDPOINT_NAME = os.environ.get("MAS_ENDPOINT_NAME", "")

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
    """Workspace host (no scheme). Optionally asserted against RKB_TARGET_HOST."""
    host = get_workspace_client().config.host or ""
    host = host.replace("https://", "").replace("http://", "").rstrip("/")
    assert_target_host(host)
    return host


def assert_target_host(host: str) -> None:
    """Optional guard: if RKB_TARGET_HOST is set, refuse any other workspace.

    Unset (the default) disables the guard, so the template runs on any workspace.
    """
    if not TARGET_HOST:
        return
    bare = host.replace("https://", "").replace("http://", "").rstrip("/")
    if TARGET_HOST not in bare:
        raise RuntimeError(
            f"Refusing workspace call to '{bare}' — expected host fragment "
            f"'{TARGET_HOST}' (set via RKB_TARGET_HOST)."
        )
