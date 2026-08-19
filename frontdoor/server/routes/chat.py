"""Chat routes: submit/poll OBO proxy to the warm MAS endpoint.

Why submit/poll: the Databricks Apps reverse
proxy enforces a non-configurable 120s per-request timeout, but the worst-case A4
fan-out takes ~130s. A single blocking POST would 504 silently at the proxy. So
POST /api/chat returns a job_id immediately (<1s), a background thread runs the
long MAS call, and GET /api/chat/{job_id} polls — each poll returns far under 120s.

OBO: the outbound MAS bearer is the END USER's forwarded
`x-forwarded-access-token`, NEVER the app service-principal token. A local-dev
fallback to the CLI/SP token applies ONLY when the header is absent.

Single-replica note (RESEARCH Assumption A2): the job map is an in-process dict.
This is sufficient for the ~10-user single-replica demo. If the app is scaled to
>1 replica, a job could land on a different replica than the poll — replace this
with a shared store before scaling.
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request
from pydantic import BaseModel

from .. import config, mas

router = APIRouter()

# In-process job map (single-replica demo — see module docstring / RESEARCH A2).
_JOBS: dict[str, dict] = {}
_EXECUTOR = ThreadPoolExecutor(max_workers=4)


class ChatIn(BaseModel):
    question: str


def _local_token() -> str:
    """Dev-only fallback: CLI/SP bearer when no forwarded user token is present.

    Used ONLY when x-forwarded-access-token is absent (local dev). In the deployed
    app the header is always present, so this path never runs there.
    """
    headers = config.get_workspace_client().config.authenticate()
    return headers["Authorization"].removeprefix("Bearer ")


def _run_job(job_id: str, question: str, user_token: str) -> None:
    """Background worker: call the MAS (OBO) and store the shaped answer.

    Streams APP -> MAS so the poller can report progress ("Searching prior R&D
    cases", "Querying ticket data", "Writing the answer") instead of a mute ~40s
    wait. The BROWSER still polls — nothing is streamed through the Apps proxy, so
    the 120s proxy limit is handled exactly as before.

    On any streaming failure this falls back to the blocking call, so a transport
    problem costs the progress labels, never the answer.

    Referenced via the `mas` module attribute so tests can monkeypatch
    `frontdoor.server.mas.invoke_mas`. Never logs the token.
    """
    def set_status(label: str) -> None:
        job = _JOBS.get(job_id)
        # Only annotate a still-running job; never resurrect a finished one.
        if job is not None and job.get("status") == "running":
            job["progress"] = label

    try:
        try:
            resp = mas.invoke_mas_streaming(question, user_token, on_status=set_status)
        except Exception:  # noqa: BLE001 — streaming is an optimization, not the contract
            set_status("Working")
            resp = mas.invoke_mas(question, user_token)
        _JOBS[job_id] = {"status": "done", **mas.shape_answer(resp)}
    except Exception as exc:  # noqa: BLE001 — surface any failure to the poller
        _JOBS[job_id] = {"status": "error", "detail": str(exc)}


@router.post("/chat")
def submit_chat(body: ChatIn, request: Request) -> dict:
    """Submit a question. Returns a job_id immediately (no synchronous MAS invoke)."""
    # OBO: prefer the forwarded end-user token; local-dev fallback only if absent.
    user_token = request.headers.get("x-forwarded-access-token")
    if not user_token:
        user_token = _local_token()

    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {"status": "running", "progress": mas.STATUS_START}
    # Run the long MAS call OFF the request path so the 120s proxy limit is never hit.
    _EXECUTOR.submit(_run_job, job_id, body.question, user_token)
    return {"job_id": job_id}


@router.get("/chat/{job_id}")
def poll_chat(job_id: str) -> dict:
    """Poll a job.

    Returns running{progress} | done{answer,citations,sources} | error{detail}.
    `progress` is a human status label reflecting how far the MAS routing has got.
    """
    return _JOBS.get(job_id, {"status": "error", "detail": "unknown job_id"})
