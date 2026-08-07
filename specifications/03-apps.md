# App — front-door chat

## Shared Context

**Target:** `{{CATALOG}}.{{SCHEMA}}`. One Databricks App — FastAPI + React, declared
as a native `apps` bundle resource.

---

## Front-door chat

`src/deploy/` + `resources/apps.yml` → app `fis-rnd-frontdoor`

The engineer-facing surface. Chat box, cited answers, clickable ticket chips.

### On-behalf-of, not service principal

The app calls the Supervisor with the **signed-in user's** forwarded token
(`x-forwarded-access-token`), never the app service principal. So a user only ever
retrieves tickets they are entitled to, and the audit trail names a person.

`user_api_scopes: [serving.serving-endpoints]` is required for this, and the DAB
App resource schema has **no field for it** — so a small post-deploy job applies it
(`fis_frontdoor_authz`). That is a genuine gap, not an oversight.

### Surviving the 120s proxy limit

The Databricks Apps reverse proxy enforces a non-configurable 120-second
per-request timeout, and the worst-case fan-out question takes longer. A single
blocking POST 504s **silently** — nothing appears in the app logs, because the
error is generated at the proxy.

So: `POST /api/chat` returns a `job_id` immediately, a background worker runs the
long call, and the browser polls. Every poll returns well under the limit.

### Progress, not a spinner

The app streams **app → Supervisor** (not browser → app, so the proxy limit is
untouched) purely to report routing progress. Measured on a 58s turn:

| Time | Status shown |
|---|---|
| 3.0s | Routing your question |
| 8.6s | Resolving terminology |
| 33.0s | Searching prior R&D cases |
| 41.2s | Writing the answer |

First feedback at ~3s instead of a mute 58-second wait.

**Progress only, deliberately.** The stream carries the whole routing trace — a
preamble, then the KA's intermediate pass, and the real answer only as the *last*
turn (~34s → ~43s on that measurement). Rendering the deltas as they arrive would
show the answer two or three times over, interleaved with agent-attribution
markers. So the deltas drive a status label and the rendered answer is still built
from the completed final turn.

### Rendering the answer

Three things the response shaping must do, each fixing a real defect:

1. **Take the final turn only.** The Responses-API `output[]` is the routing trace:
   preamble, `<name>agent</name>` markers, the KA's verbose intermediate pass, raw
   Genie table markup, then the synthesis. Joining all of it renders the answer
   several times. On a captured response that is 18,916 characters of which the
   answer is the last 3,183.
2. **Render markdown.** The agent returns `##` headers, `**bold**` and numbered
   lists. Rendered raw they show as literal syntax. Use a markdown renderer that
   does *not* pass raw HTML through, so the no-XSS guarantee holds.
3. **Strip footnote scaffolding, keep the URLs.** The KA emits pandoc-style
   `[^id]` markers plus a definition block carrying a per-ticket source URL.
   Unrendered, the markers appear literally mid-sentence. Strip the markers, but
   harvest the URLs so the citation chips become working links.

Citations are scoped to the answer, not the whole trace: the trace carries every
ticket id Genie returned in its result table (56 on one measured response, versus
the 12 the answer cites). Chips for tickets the reader never sees referenced are
noise, not evidence.

## Packaging gotcha

`source_code_path` uploads the **whole** directory. Without excludes this ships
`.venv` and `node_modules` — 214MB of junk in the measured case. Exclude them in
`sync.exclude`, but **keep** `frontend/dist`: that is the built SPA the FastAPI app
serves.
