# FieldFix: a Multi-Agent Knowledge Assistant for Field Troubleshooting & Repair

**An integration blueprint — a working, deployable Databricks multi-agent knowledge
assistant that turns your organization's historical operational records into a
searchable knowledge base for day-to-day operations. Not a throwaway demo: it runs
end-to-end on day one, then you point it at your own data.**

- Ask in plain English → cited prior cases with **real ticket numbers**
- A **Supervisor** routes each question to a Knowledge Assistant, Genie, or both
- Controlled vocabulary lives in a **governed glossary** — a term becomes extractable by being *approved*, not by a deploy
- **Domain-agnostic** — swap the corpus + glossary; pipeline, agents, and app are unchanged

## The problem

Every operations-heavy organization sits on years of hard-won troubleshooting
knowledge — tickets, resolutions, tribal know-how — that is effectively *write-only*.
The engineer who solved a failure two years ago has moved on, and their fix is buried
in free-text notes with inconsistent jargon, unfindable among thousands of records. So
teams re-diagnose from scratch, and operational questions like "how many, who owns it,
which sites, what's the backlog" stay manual queries nobody has time to write.

This is a reusable blueprint for **any** team that wants to turn that history into a
knowledge base that enhances operations — field repair is just the shipped example.
Two complementary engines cover the two kinds of questions, and neither is enough
alone: a **Knowledge Assistant** handles unstructured recall ("have we seen this,
what's the fix?") with cited prior cases, while **Genie** handles structured analytics
("how many, who, which sites, what's the backlog?") with natural-language SQL. A
**Supervisor** routes across both — the Databricks pattern for combining structured and
unstructured data access.

## Example

| | |
|---|---|
| **Org** | A roadside truck-screening equipment operator (weigh-in-motion scales, plate/DOT readers, inspection cameras) running sites across US states and Canadian provinces |
| **Corpus** | A decade of ServiceNow R&D troubleshooting tickets across those sites — the write-only history this blueprint makes searchable |
| **Ask** | "A WIM site is reporting 0 weights, have we seen this before?" → the closest prior cases **with ticket numbers**, software/config causes separated from hardware, lowest-effort-first steps. |
| **Impact** | Time-to-diagnosis drops from "search ServiceNow and hope" to one cited answer. Expert-finding and triage become SQL questions instead of tribal knowledge. |

## Key numbers (shipped example corpus)

| Metric | Value |
|---|---|
| Tickets in corpus | 23 synthetic sample tickets; extend or regenerate with the synthetic generator |
| Curated glossary terms | ~15 SME-approved seed terms (acronyms + systems/vendors), extended by mined proposals |
| Enrichment cost | one-time, a few dollars for the sample corpus; ongoing ~$0 (content-hash gated) |
| Query archetypes | 5: terminology, expert-finding, complexity/delay, priority triage, site-pattern |
| Agent surfaces | Knowledge Assistant + Genie space + Supervisor + front-door app |

## Business value

**Faster field repair turns equipment downtime back into revenue — size it with the
customer, from their numbers:**

`knowledge siloed` → `agent finds the exact prior fix` → `higher first-time-fix` →
`shorter MTTR` → **`less downtime = revenue kept`**

```
  field faults per year          [THEIR DATA]
× hours of downtime per fault    [THEIR DATA — MTTR]
× % of MTTR the agent removes    [MEASURED in your workspace]
× revenue / SLA penalty per downtime-hour   [THEY TELL YOU]
─────────────────────────────────────────────
= annual revenue protected   × attribution to Databricks (they set it)
```

Their data, their numbers, measured against manual search — that is what keeps the
number defensible.

## Architecture

A **retrieval-and-orchestration** system on Unity Catalog: one canonical Delta table is
enriched **once** by `ai_query`, and **both** retrieval engines read that same table — a
**Knowledge Assistant** for cited similar-case retrieval and a **Genie Space** for NL→SQL
— behind a **Multi-Agent Supervisor** and a Databricks One / Genie front door, entirely on
serverless.

## Documentation

| Doc | What's in it |
|---|---|
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | System overview, colored logical + component diagrams, request data flow, key abstractions, directory layout |
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | Step-by-step deployment runbook: auth → data pipeline → agents → front door → tests → teardown |
| **[specifications/](specifications/)** | Component specs: [01 ingest + enrich](specifications/01-ingest-and-enrich.md) · [02 agents](specifications/02-agents.md) · [03 apps](specifications/03-apps.md) |

**Built on Databricks:** [Agent Bricks](https://docs.databricks.com/aws/en/generative-ai/agent-bricks/) (Knowledge Assistant + Multi-Agent Supervisor) · [AI/BI Genie](https://docs.databricks.com/aws/en/genie/) · [`ai_query`](https://docs.databricks.com/aws/en/sql/language-manual/functions/ai_query) · [Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/) · [Unity Catalog](https://docs.databricks.com/aws/en/data-governance/unity-catalog/)

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
