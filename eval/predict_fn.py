#!/usr/bin/env python3
"""FIS AI Knowledge Agent — Phase 7 Plan 01: MLflow-eval predict_fn over the MAS.

The system under test is the DEPLOYED Multi-Agent Supervisor (endpoint
`mas-f5fc28b0-endpoint`, Responses API `{"input":[...]}`) — the same endpoint the
front door calls (CONTEXT LOCK: eval the endpoint, not KA/Genie in isolation).
This is the deliberate, documented exception to the skill's "import the agent
locally" guidance: there is no local MAS to import — it is a managed Agent Bricks
tile, so we wrap the proven CLI-token invocation client.

We REUSE the standalone, CLI-profile-based invocation helpers from
`src/deploy/test_supervisor.py` (`invoke_mas`, `TICKET_RE`, `prose_of`, plus the
endpoint/host/token discovery helpers) rather than reimplementing HTTP or
citation-shaping code. We do NOT import `frontdoor/server/mas.py`: it is coupled
to the Databricks Apps OBO config module and expects an `x-forwarded` end-user
token, neither of which exists in a standalone CLI harness. We reuse only its
citation-shaping CONTRACT ({response, sorted-unique R&DTASK citations}).

Security (T-07-06): the OAuth token is used only as the outbound bearer inside
the reused `invoke_mas`; it is NEVER printed, logged, or returned.
"""

import re
import sys
from pathlib import Path

import mlflow

# --- Reuse the proven standalone MAS client (mirror test_supervisor's sys.path
#     idiom: it inserts REPO_ROOT/"preflight"; we insert REPO_ROOT/"agents"). ---
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agents"))
from test_supervisor import (  # noqa: E402
    invoke_mas,
    prose_of,
    TICKET_RE,
    read_mas_endpoint,
    endpoint_ready,
    resolve_host_token,
)

# Citation-shaping CONTRACT (mirrors frontdoor/server/mas.py CITATION_RE +
# shape_answer): R&DTASK tokens surface inline in the MAS prose. test_supervisor's
# TICKET_RE is stricter (7 digits, tolerates the &-dropped form) — reuse it so a
# malformed token is never mistaken for a citation.
CITATION_RE = re.compile(r"R&DTASK\d+")  # documents the shape; TICKET_RE is used.


def build_predict_fn(profile, endpoint, host, token):
    """Return a `predict_fn(question)` closure bound to the warm MAS endpoint.

    The closure is decorated with `@mlflow.trace` so `mlflow.genai.evaluate`
    scorers see a structured trace boundary per row. The parameter name is
    `question` (NOT `inputs`) so it matches the dataset `inputs={"question": ...}`
    key — MLflow unpacks `inputs` as kwargs (Pitfall 4).

    The reused `invoke_mas` POSTs the Responses-API `{"input":[{role,content}]}`
    shape via curl `--max-time 300` with transient-error retry (handles the
    ~130s A4 fan-out latency + gateway "context canceled" blips). We NEVER use the
    chat-completions request shape (Pitfall 5 carry-forward).
    """

    @mlflow.trace
    def predict_fn(question):
        ok, resp, err = invoke_mas(endpoint, question, profile, host, token)
        if not ok or resp is None:
            # Surface the failure as an empty-but-well-formed output so the row
            # scores (poorly) rather than crashing the whole eval run. The token
            # is never included in the error text (invoke_mas does not echo it).
            return {"response": f"[MAS invocation failed: {err}]", "citations": []}
        prose = prose_of(resp)
        citations = sorted(set(TICKET_RE.findall(prose)))
        return {"response": prose, "citations": citations}

    return predict_fn
