# FieldFix: a Multi-Agent Knowledge Assistant for Field Troubleshooting & Repair

> **What this is.** A Databricks solution template for a multi-agent knowledge
> assistant over any organization's historical maintenance & repair troubleshooting
> tickets. A field engineer asks a question in plain English; a **Supervisor**
> routes it to a **Knowledge Assistant** for similar-case retrieval with real
> ticket citations, or to **Genie** for counts, expert-finding and triage, or
> both. The agent's controlled vocabulary comes from a **governed glossary**, not
> from code, so a term becomes extractable by being approved, not by a deploy. The
> domain is swappable: point it at your own ticket corpus and glossary and the
> pipeline, agents, and app are unchanged.

## The example story

> The template is domain-agnostic. It ships with the worked example below so you
> have something live to explore on day one; swap the corpus and glossary and the
> same machinery serves any maintenance & repair operation.

| | |
|---|---|
| **Example org** | A roadside truck-screening equipment operator (weigh-in-motion scales, plate/DOT readers, inspection cameras) running sites across US states and Canadian provinces |
| **Hero** | A field or R&D engineer holding an open, unresolved task |
| **Problem** | A decade of troubleshooting history in ServiceNow is effectively write-only. The engineer who solved this exact failure two years ago has moved on; their ticket is unfindable among thousands. So the team re-diagnoses from scratch, repeatedly. |
| **Investigation** | Ask the agent "a WIM site is reporting 0 weights, have we seen this before?" It returns the closest prior cases **with ticket numbers**, separates software/config causes from hardware causes, and ends with lowest-effort-first steps. |
| **Root cause (of the org problem)** | The knowledge existed the whole time. It was unretrievable because the useful content was buried in free-text notes with inconsistent jargon, and nothing mapped an acronym to its meaning. |
| **Impact** | Time-to-diagnosis drops from "search ServiceNow and hope" to one cited answer. Expert-finding and triage become SQL questions instead of tribal knowledge. |

---

## Overview

Every ticket is enriched **once** by an LLM into canonical structured columns:
`systems_involved`, `vendors`, `problem_category`, `root_cause`,
`resolution_type`, plus the free-text description segmented by *meaning* into
`summary` / `customer_impact` / `troubleshooting` / `recommendation`. That
segmentation is what makes the corpus retrievable: a raw ServiceNow description
is one wall of text mixing symptom, impact, what was tried and what to do next.

Both engines then read **one physical table**. The Knowledge Assistant indexes a
single pre-composed content column and cites via a `metadata` struct; Genie reads
the structured columns of the same rows through a commented view. Same rows, two
access patterns — so a semantic answer and an aggregate answer can never
disagree about the underlying data.

The governance thread is the part worth slowing down on in a demo. The
systems/vendors enums the LLM is allowed to emit are read **at run time** from
the glossary, filtered to `status='approved'`. Approve a term and the next
enrichment run can extract it. There is no hardcoded term list anywhere, and a
drift guard proves it by asserting the enrichment's system values and the approved
glossary set are identical in both directions.

---

## Key Numbers (shipped example corpus)

| Metric | Value |
|---|---|
| Tickets in corpus | 23 synthetic sample tickets (one row per task); extend or regenerate with the synthetic generator |
| Curated glossary terms | ~15 SME-approved seed terms (expandable acronyms plus systems/vendors), extended by mined proposals |
| Enrichment cost | one-time, a few dollars for the sample corpus; ongoing ~$0 (content-hash gated) |
| Query archetypes covered | 5: terminology, expert-finding, complexity/delay, priority triage, site-pattern |
| Agent surfaces | Knowledge Assistant + Genie space + Supervisor + front-door app |

---

## Business Value — closing the loop

**Faster field repair turns asset/equipment downtime back into revenue. Size it
with the customer, from their numbers.**

Where the value comes from:

`knowledge siloed across systems` → `agent finds the exact prior fix` →
`faster diagnosis, higher first-time-fix` → `shorter MTTR, fewer repeat dispatches` →
**`less downtime = revenue kept`**

### The revenue-loss-avoidance equation

```
  field faults per year          [THEIR DATA]
× hours of downtime per fault    [THEIR DATA — MTTR]
× % of MTTR the agent removes    [MEASURED in the demo]
× revenue / SLA penalty per downtime-hour   [THEY TELL YOU]
─────────────────────────────────────────────
= annual revenue protected   × attribution to Databricks (they set the %)
```

**Split the inputs honestly — it is what makes the number defensible:**

| Input | Who supplies it | Where it comes from |
|---|---|---|
| Fault volume, MTTR, first-time-fix rate | **Their data** | computed from their own ticket history |
| % of MTTR the agent removes | **Measured** | the demo — time-to-cited-answer vs. manual search |
| Revenue / SLA penalty per downtime-hour | **They tell you** | only the business can set this; never invent it |
| Attribution to Databricks | **They set it** | their call, not yours |


---


## Running it in your own workspace

Follow DEPLOYMENT.MD

---

## License

© 2026 Databricks, Inc. All rights reserved. The source in this project is provided
subject to the Databricks License (see [`LICENSE.md`](LICENSE.md)). All included or referenced
third-party libraries are subject to their own licenses.

## Disclaimer

This project and its contents are provided **as-is**, for demonstration purposes only, and
are **not** formally supported by Databricks under any Service Level Agreements. It is
intended as a solution accelerator you adapt to your own data and workspace; deploy and run
it at your own risk. Nothing here constitutes a commitment to deliver any feature or
functionality.
