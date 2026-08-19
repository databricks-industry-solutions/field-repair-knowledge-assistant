"""MAS invocation + response shaping for the front door.

Two responsibilities, and nothing else (all retrieval/reasoning is in the MAS):
  * invoke_mas(question, user_token) — POST the Responses-API `{"input":[...]}`
    shape to the warm MAS endpoint under the END USER's OBO bearer token, with a
    long client timeout (300s). Runs only in a background worker (never on a
    proxied request path) so the 120s Apps proxy limit is never hit.
  * shape_answer(resp) — parse the Responses-API `output[]` into the FINAL answer
    + the sorted-unique set of R&DTASK citations, each with a source URL.

Why the final answer only: MAS streams its whole routing trace through `output[]` —
a preamble ("Let me start by querying..."), a standalone `<name>agent</name>` marker
before each delegated turn, the KA's verbose intermediate pass, raw Genie table
markup, and finally the supervisor's synthesis. Joining all of it (the previous
behavior) rendered the answer two or three times over, interleaved with agent tags
and pipe-delimited tables. On the captured A5 fixture that is 18,916 characters of
which the actual answer is the last 3,184.

Security: the user token is used only as the outbound bearer
and is NEVER logged, printed, or returned to the client. There is NO fallback to
the app service-principal token here — the caller supplies the user token.
"""
import json
import re

import requests

from . import config

# R&DTASK citations appear as literal tokens in the MAS prose (05 invocation contract A3).
CITATION_RE = re.compile(r"R&DTASK\d+")

# A standalone `<name>agent-id</name>` message marks which sub-agent speaks next.
# It is routing metadata, never prose.
AGENT_NAME_RE = re.compile(r"<name>\s*[^<>]*\s*</name>")

# KA emits pandoc-style footnotes: inline `[^kcNX-1]` markers plus a definition
# block `[^kcNX-1]: quoted evidence https://...`. Nothing rendered them, so both
# leaked as literal text — including mid-sentence ("14 months vs. [^kcNX-9]12-month").
FOOTNOTE_MARKER_RE = re.compile(r"[ \t]*\[\^[\w-]+\]")
FOOTNOTE_DEF_RE = re.compile(r"^[ \t]*\[\^[\w-]+\]:.*(?:\n(?![ \t]*\[\^)[^\n]*)*", re.MULTILINE)

# Ticket URL inside a footnote definition, e.g.
#   .../ajax-api/2.0/fs/files/synthetic/R%26DTASK0002127.md
# The ticket id round-trips through URL-encoding, so match either form.
FOOTNOTE_URL_RE = re.compile(r"(https?://[^\s\)\]]*?(R(?:&|%26)DTASK\d+)[^\s\)\]]*)")


def invoke_mas(question: str, user_token: str) -> dict:
    """Call the warm MAS endpoint on behalf of the end user (OBO).

    Args:
        question:   the user's question (bound as a JSON value — no interpolation).
        user_token: the end user's OAuth token (from x-forwarded-access-token).
                    This is the outbound bearer — NEVER the app SP token.

    Returns the raw Responses-API JSON body. Never logs the token.
    """
    host = config.get_host()  # asserts the target host
    url = f"https://{host}/serving-endpoints/{config.MAS_ENDPOINT_NAME}/invocations"
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {user_token}",  # USER token, not the SP token
            "Content-Type": "application/json",
        },
        # Responses API (task=agent/v1/responses): input[] shape — NOT the
        # chat-completions messages shape.
        json={"input": [{"role": "user", "content": question}]},
        timeout=300,  # MAS_TIMEOUT_S — background worker only; proxied path uses submit/poll
    )
    resp.raise_for_status()
    return resp.json()


# --- Streaming (progress-only) ----------------------------------------------
# The endpoint supports SSE via {"stream": true}. We stream APP -> MAS only; the
# browser still uses submit/poll, so the Apps proxy never holds a long-lived
# streamed request and the non-configurable 120s proxy limit stays handled exactly
# as before.
#
# Why progress-only and not streamed prose: the stream mirrors the ROUTING TRACE.
# Measured on the A5-style WIM question, the first text arrives at ~4s but it is
# the preamble ("I'll help you find..."), then ~27s of the KA's intermediate pass,
# and the real answer is the LAST turn (~34s -> ~43s). Rendering deltas as they
# arrive would undo the trace-suppression fix. So deltas drive a status label and
# the rendered answer is still built by shape_answer from the completed items.

# Sub-agent id -> the status line shown while it runs. Matched as substrings
# because the ids are uuid-suffixed (e.g. "ka-97df484b-f50a-...").
_AGENT_STATUS = (
    ("ka-", "Searching prior R&D cases"),
    ("genie-", "Querying ticket data"),
    ("glossary", "Resolving terminology"),
)
STATUS_START = "Routing your question"
STATUS_WRITING = "Writing the answer"


def _status_for_call(name: str) -> str:
    """Map a function_call tool name to a human status line."""
    lowered = (name or "").lower()
    for prefix, label in _AGENT_STATUS:
        if prefix in lowered:
            return label
    return "Consulting tools"


def _iter_sse(resp) -> "list[dict]":
    """Yield parsed SSE `data:` payloads from a streamed response.

    Buffers by BLANK-LINE record boundary, not by line: some payloads contain
    embedded newlines, and splitting per line corrupts them (verified live — a
    line-based split left an unparseable fragment).
    """
    buf: list[str] = []
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw.rstrip("\r")
        if line == "":  # record boundary
            if buf:
                payload = "\n".join(buf)
                buf = []
                if payload.strip() and payload.strip() != "[DONE]":
                    try:
                        yield json.loads(payload)
                    except json.JSONDecodeError:
                        pass  # a keep-alive or partial record — skip, never crash
            continue
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
        elif buf:
            buf.append(line)  # continuation of a multi-line data payload
    if buf:
        payload = "\n".join(buf)
        if payload.strip() and payload.strip() != "[DONE]":
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                pass


def invoke_mas_streaming(question: str, user_token: str, on_status=None) -> dict:
    """Call the MAS with SSE and report progress, returning the same body shape.

    `on_status(label)` is invoked as the routing progresses. The return value is a
    reconstructed `{"output": [...]}` built from `response.output_item.done` events
    — each carries the completed item with the SAME shape as the non-streaming
    `output[]`, so `shape_answer` consumes it unchanged (verified against a live
    capture: 6 items, identical clean answer).

    Falls back to nothing: on any streaming failure the CALLER decides (see
    routes.chat), so a stream problem degrades to the blocking path.
    """
    host = config.get_host()  # asserts the target host
    url = f"https://{host}/serving-endpoints/{config.MAS_ENDPOINT_NAME}/invocations"
    output: list[dict] = []
    if on_status:
        on_status(STATUS_START)
    with requests.post(
        url,
        headers={
            "Authorization": f"Bearer {user_token}",  # USER token, not the SP token
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        json={"input": [{"role": "user", "content": question}], "stream": True},
        stream=True,
        timeout=300,
    ) as resp:
        resp.raise_for_status()
        saw_answer_text = False
        for event in _iter_sse(resp):
            etype = event.get("type", "")
            if etype == "response.output_item.done":
                item = event.get("item")
                if not item:
                    continue
                output.append(item)
                if item.get("type") == "function_call":
                    if on_status:
                        on_status(_status_for_call(item.get("name", "")))
                continue
            if etype == "response.output_text.delta" and not saw_answer_text:
                # First prose after the tools have reported: the synthesis is being
                # written. Only flip once the tools have run, otherwise the opening
                # preamble would claim we are already writing the answer.
                if any(o.get("type") == "function_call" for o in output):
                    saw_answer_text = True
                    if on_status:
                        on_status(STATUS_WRITING)
    return {"output": output}


def _prose_join(resp: dict) -> str:
    """Join the assistant message text across output[] items (05 contract A3).

    This is the FULL trace, agent markers and all. Used for citation/source
    harvesting, where the footnote block in an intermediate turn is the only place
    the ticket URLs appear. Not what we render.
    """
    return "".join(
        c["text"]
        for o in resp.get("output", [])
        for c in (o.get("content") or [])
        if isinstance(c, dict) and "text" in c
    )


def _text_items(resp: dict) -> list[str]:
    """Per-item assistant text, in order — one string per output[] message."""
    items = []
    for o in resp.get("output", []):
        if o.get("type") != "message":
            continue  # function_call items carry no prose
        text = "".join(
            c["text"]
            for c in (o.get("content") or [])
            if isinstance(c, dict) and "text" in c
        )
        if text.strip():
            items.append(text)
    return items


def _final_answer(resp: dict) -> str:
    """The last substantive assistant turn — the supervisor's synthesis.

    Skips from the end past anything that is not prose: the `<name>` routing
    markers, and raw Genie result tables (leading `||col|col|` pipe markup). Falls
    back to the whole join only if nothing qualifies, so a shape we have not seen
    degrades to the old behavior rather than to an empty answer.
    """
    for text in reversed(_text_items(resp)):
        stripped = AGENT_NAME_RE.sub("", text).strip()
        if not stripped:
            continue  # a bare <name> marker
        if stripped.startswith("||") or stripped.startswith("|-"):
            continue  # raw Genie table markup
        return stripped
    return _prose_join(resp).strip()


def clean_prose(text: str) -> str:
    """Strip routing markers and footnote scaffolding from rendered prose.

    Footnote definitions go first (they contain markers), then inline markers, then
    the blank-line pileup left behind. Markdown itself is preserved — the client
    renders it.
    """
    text = FOOTNOTE_DEF_RE.sub("", text)
    text = AGENT_NAME_RE.sub("", text)
    text = FOOTNOTE_MARKER_RE.sub("", text)
    # Collapse 3+ newlines to a paragraph break, and drop trailing spaces per line.
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_sources(resp: dict) -> dict:
    """Map ticket id -> workspace URL, harvested from the footnote definitions.

    The KA's footnote block is the only place a per-ticket URL appears, so this
    reads the FULL trace even though we render only the final turn. Ids are
    normalized to the literal `R&DTASK…` form; the URL is kept verbatim (its
    `%26` encoding is what makes the link resolve).
    """
    sources = {}
    for url, raw_id in FOOTNOTE_URL_RE.findall(_prose_join(resp)):
        ticket = raw_id.replace("%26", "&")
        sources.setdefault(ticket, url)
    return sources


def shape_answer(resp: dict) -> dict:
    """Shape the MAS Responses-API body into {answer, citations, sources}.

    * answer    — the final assistant turn, cleaned of agent markers and footnote
                  scaffolding. Markdown is intact for the client to render.
    * citations — SORTED, UNIQUE R&DTASK ids cited IN THE ANSWER. Scoped to the
                  answer on purpose: the full trace contains every id Genie
                  returned in its result table (56 on the A5 fixture, vs 12 the
                  answer actually cites). Chips for tickets the reader cannot see
                  referenced are noise, not a trust signal.
    * sources   — {ticket_id: url} for the citations we found a footnote URL for.
                  Possibly empty, and a subset of `citations`; the client must not
                  assume a URL exists for a given id.
    """
    answer = clean_prose(_final_answer(resp))
    citations = sorted(set(CITATION_RE.findall(answer)))
    all_sources = extract_sources(resp)
    sources = {t: u for t, u in all_sources.items() if t in citations}
    return {"answer": answer, "citations": citations, "sources": sources}
