#!/usr/bin/env python3
"""Field Repair Knowledge Assistant — Phase 7 Plan 02: custom citation_groundedness scorer.

The groundedness/citation dimension of EVAL-02. The built-in
retrieval-groundedness scorer CANNOT be used here: the deployed MAS endpoint
emits inline prose citations + `function_call` tool spans, NOT MLflow `Document`
RETRIEVER spans (RESEARCH Pitfall 5 / Assumption A1). So groundedness is a custom
`@scorer` that:

  (a) if the answer makes claims with ZERO R&DTASK citations -> "no";
  (b) resolves every cited R&DTASK number against the live corpus
      (`rd_tasks_gold_analytics.task_number`, reusing `tickets_in_corpus`); any
      hallucinated / non-corpus citation -> "no", naming the offenders;
  (c) otherwise fetches the cited tickets' `case_text` and asks an LLM judge
      (`meets_guidelines`) whether every factual claim is supported by that
      cited text — the "supports-the-claim" verdict.

Citation-resolution primitives (`tickets_in_corpus`, `_norm_ticket`, `TICKET_RE`,
`run_sql`, `WAREHOUSE_ID`, `CATALOG`, `SCHEMA`) are REUSED verbatim from
`src/deploy/test_supervisor.py` — never reimplemented.

Live corpus columns (confirmed at build time via information_schema on
`the reference workspace`, 2026-07-30):
  - citation resolution: `rd_tasks_gold_analytics.task_number` (Assumption A5)
  - claim-support text:   `rnd_tickets(number, case_text)`  ← the citeable column
"""

import sys
from pathlib import Path

from mlflow.genai.scorers import scorer
from mlflow.genai.judges import meets_guidelines
from mlflow.entities import Feedback

# --- Reuse the proven citation-resolution primitives (do NOT reimplement) ----
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agents"))
from test_supervisor import (  # noqa: E402
    tickets_in_corpus,
    _norm_ticket,
    TICKET_RE,
    run_sql,
    WAREHOUSE_ID,
    CATALOG,
    SCHEMA,
)

# The `@scorer` signature cannot take a `profile` kwarg (MLflow only passes
# inputs/outputs/expectations/trace), so run_eval sets this module-level PROFILE
# before calling evaluate. Defaults to the standard demo profile.
PROFILE = "serverless-stable"

# The judge that grades "supports the claim". model="databricks" routes to the
# platform-managed judge FM via the first-party databricks-agents client. A pinned
# `databricks:/databricks-claude-sonnet-4-5` URI instead requires the LiteLLM
# client adapter — an un-audited package forbidden by T-07-SC (07-01 deviation
# [Rule 3 - blocking], carried forward + runtime-confirmed: meets_guidelines with
# the pinned URI raises "install litellm", the managed judge returns yes/no).
JUDGE_MODEL = "databricks"

# The live citeable text column (confirmed): rnd_tickets(number, case_text).
TICKETS_TABLE = f"{CATALOG}.{SCHEMA}.rnd_tickets"


def fetch_case_text(numbers, profile):
    """Fetch the concatenated `case_text` for the given corpus ticket numbers,
    for use as the judge's 'supports-the-claim' context. Read-only SELECT."""
    nums = sorted({_norm_ticket(n) for n in numbers})
    if not nums:
        return ""
    in_list = ",".join("'" + n.replace("'", "''") + "'" for n in nums)
    state, rows = run_sql(
        f"SELECT number, case_text FROM {TICKETS_TABLE} WHERE number IN ({in_list})",
        profile, WAREHOUSE_ID)
    if state != "SUCCEEDED" or not rows:
        return ""
    return "\n\n".join(f"[{r[0]}]\n{r[1]}" for r in rows if r and r[1])


def score_citation_groundedness(inputs, outputs, profile=None):
    """Pure, testable core of the citation_groundedness scorer (returns a
    Feedback). The `@scorer`-decorated wrapper below binds `profile` from the
    module-level PROFILE. Kept separate so the deterministic branches (zero /
    hallucinated citations) are unit-testable offline (eval/test_scorers.py)."""
    profile = profile or PROFILE
    cites = list(outputs.get("citations") or [])

    # (a) claims with zero citations -> not grounded.
    if not cites:
        return Feedback(
            name="citation_groundedness", value="no",
            rationale="Answer made factual claims with zero R&DTASK citations.")

    # (b) every cite must resolve in the live corpus (not hallucinated).
    resident = tickets_in_corpus(cites, profile)
    unresolved = [c for c in cites if _norm_ticket(c) not in resident]
    if unresolved:
        return Feedback(
            name="citation_groundedness", value="no",
            rationale=("Hallucinated / non-corpus citation(s) that do not "
                       f"resolve in {CATALOG}.{SCHEMA}.rd_tasks_gold_analytics: "
                       f"{unresolved}"))

    # (c) all cites resolve -> ask the judge if the cited text supports the claims.
    cited_text = fetch_case_text(sorted(resident), profile)
    if not cited_text:
        return Feedback(
            name="citation_groundedness", value="no",
            rationale=("Citations resolve in the corpus but their case_text "
                       "could not be fetched to verify claim support."))
    return meets_guidelines(
        name="citation_groundedness",
        guidelines=["Every factual claim in the response must be supported by "
                    "the cited ticket text provided in retrieved_documents."],
        context={"request": inputs.get("question", ""),
                 "response": outputs.get("response", ""),
                 "retrieved_documents": cited_text},
        model=JUDGE_MODEL,   # managed judge (see JUDGE_MODEL note)
    )


@scorer
def citation_groundedness(inputs, outputs):
    """MLflow scorer: resolves every R&DTASK citation against the live corpus and
    judges claim support. One distinct MLflow metric key: citation_groundedness.
    The built-in retrieval-groundedness scorer is NOT used (no RETRIEVER span —
    Pitfall 5)."""
    return score_citation_groundedness(inputs, outputs, PROFILE)
