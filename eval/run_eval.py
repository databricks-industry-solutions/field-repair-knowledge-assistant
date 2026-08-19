#!/usr/bin/env python3
"""Field Repair Knowledge Assistant — Phase 7 Plan 02: full on-wording MLflow GenAI eval.

Broadens the 07-01 thin A1 slice into the full 5-archetype on-wording evaluation
against the deployed Multi-Agent Supervisor (`the MAS endpoint`, Responses
API): host-gate → assert the warm endpoint is READY (reuse, never re-provision) →
build the claim-decomposed 5-archetype dataset from `Prompts.md` (via
`eval/dataset.py`, `RKB_PROMPTS_PATH`, in-memory only) → `mlflow.genai.evaluate`
with THREE separate dimension scorers plus a plausible-reasoning Guidelines
scorer → assert three DISTINCT dimension metrics were produced (EVAL-02 "graded
separately") and print all of their means.

Scorers (each → its own distinct MLflow metric key — never one blended score):
  - Correctness            (dimension 1: claim-level, vs expectations.expected_facts)
  - RelevanceToQuery       (dimension 2: does the answer address the question)
  - citation_groundedness  (dimension 3: custom @scorer — resolves every R&DTASK
                            cite against the live corpus + judges claim support;
                            built-in RetrievalGroundedness cannot fire, Pitfall 5)
  - plausible_reasoning    (Guidelines scorer carrying EVAL-01's plausible-
                            reasoning/hedge/citation allowance)

Design mirrors the repo harness convention (`src/deploy/test_supervisor.py`):
  - Step 0 host-safety gate (`preflight.assert_target_host`) — refuses any
    workspace but the reference workspace (T-07-03).
  - Warm-endpoint READY guard — exits non-zero rather than re-provisioning; a
    forced re-provision would reset the per-tile SSP (T-07-08). This harness
    only ever reuses the warm endpoint by name.
  - Standalone, CLI-profile-based; `--profile` / `--only`; non-zero exit on failure.

Leakage guard (T-07-01): the dataset `expected_facts` are CONCEPTUAL capability
claims (CA → Controller Application is public in src/deploy/glossary.md); the private
`Prompts.md` answer key is read in-memory only and never written to disk.

Usage:
    python3 eval/run_eval.py --profile serverless-stable            # all 5
    python3 eval/run_eval.py --profile serverless-stable --only A1  # smoke
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Reuse the Phase-1 host-safety gate + the standalone MAS discovery helpers.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "preflight"))
sys.path.insert(0, str(REPO_ROOT / "agents"))
sys.path.insert(0, str(REPO_ROOT / "eval"))
from preflight import assert_target_host, resolve_principal  # noqa: E402
from test_supervisor import (  # noqa: E402
    read_mas_endpoint,
    endpoint_ready,
    resolve_host_token,
)
from predict_fn import build_predict_fn  # noqa: E402
from dataset import build_onwording, ARCHETYPE_IDS  # noqa: E402
from shadow_loader import load_shadow  # noqa: E402
import scorers as eval_scorers  # noqa: E402
from scorers import citation_groundedness  # noqa: E402

EXPERIMENT = "/Shared/rkb-eval"

# The phase report the --shadow run writes (in-repo, committed). It carries
# metrics + verdicts ONLY — never verbatim Prompts.md answer-key text or the
# held-out real entities (T-07-01; grep-gated).
RESULTS_PATH = (
    REPO_ROOT / ".planning" / "phases" / "07-evaluation" / "07-EVAL-RESULTS.md"
)

# Overfit tolerance: a per-dimension on-wording-minus-shadow mean drop larger than
# this flags OVERFIT (documented in the report). 0.15 = ~one graded row out of the
# small sets moving a dimension; below the honest 07-02 on-wording spread noise.
OVERFIT_TOLERANCE = 0.15

# Managed judge for the built-in Correctness dimension. model_uri "databricks"
# routes the LLM-judge call server-side to the platform's managed judge (a
# Databricks Foundation Model) needing only the first-party databricks-agents
# client — no LiteLLM. (07-01 deviation [Rule 3 - blocking], carried forward: a
# specific `databricks:/<endpoint>` URI requires the LiteLLM adapter, an un-audited
# package forbidden by T-07-SC. The custom citation_groundedness scorer's
# meets_guidelines judge DOES pin `databricks:/databricks-claude-sonnet-4-5` — it
# runs via databricks-agents server-side and does not need LiteLLM.)
JUDGE_MODEL = "databricks"


def main():
    ap = argparse.ArgumentParser(
        description="Full on-wording MLflow GenAI eval over the deployed MAS")
    ap.add_argument("--profile", default="serverless-stable")
    ap.add_argument("--only", default="",
                    help="comma-separated archetype id(s) to evaluate, e.g. "
                         "A1 or A1,A4 (default: all 5)")
    ap.add_argument("--shadow", action="store_true",
                    help="ALSO run the 15 pre-existing BLIND Phase-3 shadow "
                         "prompts (03-SHADOW-PROMPTS.md) as a second named MLflow "
                         "run with the ground-truth-free scorers, compare means "
                         "per dimension (overfit check + judge-leniency guard), "
                         "and write 07-EVAL-RESULTS.md (EVAL-03).")
    args = ap.parse_args()

    only = {s.strip().upper() for s in args.only.split(",") if s.strip()}
    unknown = only - set(ARCHETYPE_IDS)
    if unknown:
        print(f"FATAL: --only {sorted(unknown)} not in the archetype set "
              f"{ARCHETYPE_IDS}.", file=sys.stderr)
        sys.exit(2)

    # Step 0 — never query the wrong/unauthenticated workspace (T-07-03).
    host = assert_target_host(args.profile)
    principal = resolve_principal(args.profile)
    print(f"Host gate OK: {host}")
    print(f"Demo principal: {principal}")

    # Reuse the warm endpoint by name (single source of truth: 05-SUPERVISOR-BUILD.md).
    endpoint = read_mas_endpoint()
    if not endpoint:
        print("FATAL: could not read MAS endpoint name from the build doc.",
              file=sys.stderr)
        sys.exit(3)
    ready, state, task = endpoint_ready(args.profile, endpoint)
    if not ready:
        print(f"FATAL: MAS endpoint {endpoint} not READY (state={state}). "
              "Do NOT re-provision — a re-create resets the per-tile SSP "
              "(05-01 carry-forward). Escalate instead.", file=sys.stderr)
        sys.exit(4)
    print(f"MAS endpoint {endpoint} READY (task={task}) — reusing warm.")

    host_url, token = resolve_host_token(args.profile)
    if not host_url or not token:
        print("FATAL: could not resolve host/OAuth token for the MAS invocation.",
              file=sys.stderr)
        sys.exit(5)

    # Cap eval parallelism to ONE worker BEFORE importing/calling evaluate. Beyond
    # the Pitfall-3 gateway-cancellation concern, 07-02 observed a transient CLI
    # OAuth-cache refresh 401 race under concurrency (exit status 45 + 401 trace
    # warning) that intermittently dropped 1-3 rows' scorer calls. Serializing the
    # rows (MAX_WORKERS=1) removes the concurrent token-refresh contention so ALL
    # rows score — mandatory for the 07-03 full-coverage overfit comparison
    # (20 rows = 5 on-wording + 15 shadow; still minutes on the warm endpoint).
    # Env var name runtime-verified against the installed mlflow
    # (mlflow.environment_variables.MLFLOW_GENAI_EVAL_MAX_WORKERS).
    os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", "1")

    import mlflow  # imported after the env cap is set
    from mlflow.genai.scorers import Correctness, RelevanceToQuery, Guidelines

    # Point MLflow at the SAME profile the harness host-gated, not whatever the
    # ambient ~/.databrickscfg default is (which may be a different workspace/PAT).
    # The profile-qualified tracking URI keeps MLflow on the reference workspace (T-07-03 —
    # otherwise the run would silently land in the wrong workspace).
    mlflow.set_tracking_uri(f"databricks://{args.profile}")
    mlflow.set_experiment(EXPERIMENT)

    # The custom citation_groundedness scorer resolves cites via SQL under this
    # profile; its @scorer signature can't take a profile kwarg, so set it here.
    eval_scorers.PROFILE = args.profile

    predict_fn = build_predict_fn(args.profile, endpoint, host_url, token)

    # Build the full 5-archetype (or --only subset) on-wording dataset from the
    # private Prompts.md answer key (in-memory; never written to disk).
    dataset = build_onwording(profile=args.profile, only=only or None)
    label = ",".join(sorted(only)) if only else "all 5 archetypes"
    print(f"\nBuilt on-wording dataset: {len(dataset)} row(s) ({label}).")

    # Warm-up: fire ONE predict_fn call before evaluate so the first eval row does
    # not pay cold-start latency (cold A4 ~139s; warm ~75s — STATE.md). Pitfall 3.
    print("Warming up the endpoint with one call (cold start can take ~130s)...")
    warm = predict_fn(dataset[0]["inputs"]["question"])
    print(f"Warm-up returned {len(warm.get('response', ''))} chars, "
          f"citations={warm.get('citations')}")

    # plausible-reasoning Guidelines scorer — carries EVAL-01's plausible-
    # reasoning/hedge/citation allowance. Guidelines auto-extracts request/response
    # from the trace; per-row expectations.guidelines sharpen the rubric.
    # Uses the managed judge (model=JUDGE_MODEL="databricks") — a pinned
    # databricks:/ URI requires LiteLLM (T-07-SC forbidden; 07-01 carry-forward).
    plausible_reasoning = Guidelines(
        name="plausible_reasoning",
        guidelines=("The response's reasoning must be plausible and must follow "
                    "the row's stated expectations.guidelines: hedge uncertain "
                    "definitions, cite grounding tickets, and never fabricate "
                    "facts absent from the corpus."),
        model=JUDGE_MODEL,
    )

    # Ground-truth-FREE scorer set — shared by BOTH runs (the shadow set has no
    # expected_facts, so Correctness is EXCLUDED from it, GOTCHAS: it would error).
    # The on-wording run ADDS Correctness on top of this set.
    gt_free_scorers = [
        RelevanceToQuery(),        # dimension 2
        citation_groundedness,     # dimension 3 (custom)
        plausible_reasoning,       # EVAL-01 plausible-reasoning / hedge Guidelines
    ]

    print(f"\nRunning on-wording mlflow.genai.evaluate on {len(dataset)} row(s) "
          f"(correctness judge={JUDGE_MODEL}; three dimension scorers + "
          "plausible_reasoning)...")
    with mlflow.start_run(run_name="onwording"):
        results = mlflow.genai.evaluate(
            data=dataset,
            predict_fn=predict_fn,
            scorers=[Correctness(model=JUDGE_MODEL)] + gt_free_scorers,
        )

    print(f"\nrun_id: {results.run_id}")
    metrics = results.metrics or {}
    print(f"metrics: {metrics}")

    # EVAL-02: assert THREE DISTINCT dimension metrics were produced (correctness,
    # relevance, citation_groundedness) — graded SEPARATELY, never one blended
    # score. Exit non-zero if any dimension is missing.
    dimensions = {
        "correctness": [k for k in metrics if "correctness" in k.lower()],
        "relevance": [k for k in metrics if "relevance" in k.lower()],
        "citation_groundedness": [k for k in metrics
                                  if "citation_groundedness" in k.lower()],
    }
    missing = [dim for dim, keys in dimensions.items() if not keys]
    print("\nDimension metrics:")
    for dim, keys in dimensions.items():
        for k in keys:
            print(f"  {k} = {metrics[k]}")
        if not keys:
            print(f"  {dim}: MISSING")
    # plausible_reasoning is scored HERE too (EVAL-01) — surface its mean.
    for k in [k for k in metrics if "plausible_reasoning" in k.lower()]:
        print(f"  {k} = {metrics[k]}")

    if missing:
        print(f"\nFATAL: EVAL-02 unmet — missing dimension metric(s): {missing}. "
              "The three dimensions must each produce a distinct metric.",
              file=sys.stderr)
        sys.exit(6)

    print(f"\nEVAL-01/02: three distinct dimension metrics (correctness, "
          f"relevance, citation_groundedness) + plausible_reasoning produced for "
          f"{len(dataset)} archetype row(s) against {endpoint}.")

    if not args.shadow:
        print("\n(--shadow not set — skipping the EVAL-03 overfit comparison. "
              "Re-run with --shadow to run the blind holdout + write "
              "07-EVAL-RESULTS.md.)")
        sys.exit(0)

    # --- EVAL-03: blind shadow holdout run + overfit comparison ---------------
    shadow = load_shadow()
    print(f"\nLoaded {len(shadow)} blind Phase-3 shadow prompt(s) "
          "(03-SHADOW-PROMPTS.md — RUN, never regenerated).")

    print(f"\nRunning shadow mlflow.genai.evaluate on {len(shadow)} row(s) "
          "(ground-truth-FREE scorers only — Correctness EXCLUDED, no "
          "expected_facts)...")
    with mlflow.start_run(run_name="shadow"):
        r_sh = mlflow.genai.evaluate(
            data=shadow,
            predict_fn=predict_fn,
            scorers=gt_free_scorers,
        )
    sh_metrics = r_sh.metrics or {}
    print(f"\nshadow run_id: {r_sh.run_id}")
    print(f"shadow metrics: {sh_metrics}")

    comparison, overfit, leniency = compare_runs(metrics, sh_metrics)

    print("\nOn-wording vs shadow overfit comparison "
          f"(tolerance={OVERFIT_TOLERANCE}):")
    for row in comparison:
        print(f"  {row['dimension']:<24} on-wording={row['onwording']!s:<8} "
              f"shadow={row['shadow']!s:<8} delta={row['delta']!s:<8} "
              f"{row['verdict']}")
    verdict = "OVERFIT" if overfit else "NOT OVERFIT"
    print(f"\nOverfit verdict: {verdict}")
    if leniency:
        print("JUDGE-LENIENCY WARNING: every on-wording dimension scored ~1.0 "
              "with no on-wording-vs-shadow spread — the judge may be lenient. "
              "Escalate the judge model to a stronger reasoning model and re-run "
              "before trusting these scores.")

    write_results_report(host, endpoint, metrics, sh_metrics, comparison,
                         overfit, leniency)
    print(f"\nWrote {RESULTS_PATH}")

    print("\nEVAL-03: blind shadow holdout run + on-wording-vs-shadow overfit "
          f"comparison complete ({verdict}"
          + ("; LENIENCY WARNING" if leniency else "") + ").")
    sys.exit(0)


# Shared dimensions compared across the two runs (ground-truth-free — present in
# BOTH the on-wording and the shadow metrics). Correctness is on-wording-only.
_SHARED_DIMS = [
    ("relevance", "relevance"),
    ("citation_groundedness", "citation_groundedness"),
    ("plausible_reasoning", "plausible_reasoning"),
]


def _find_mean(metrics, needle):
    """Return the first `<...>/mean` metric whose key contains `needle`, else None."""
    for k, v in (metrics or {}).items():
        kl = k.lower()
        if needle in kl and kl.endswith("/mean"):
            return v
    # fall back to any key containing the needle (some scorers emit non-/mean keys)
    for k, v in (metrics or {}).items():
        if needle in k.lower():
            return v
    return None


def compare_runs(on_metrics, sh_metrics, tolerance=OVERFIT_TOLERANCE):
    """Compute the per-dimension on-wording-vs-shadow comparison + verdicts.

    Returns (comparison_rows, overfit_flag, leniency_flag):
      - comparison_rows: list of {dimension, onwording, shadow, delta, verdict}
        for each ground-truth-free dimension present in BOTH runs;
      - overfit_flag: True if any shared dimension's (on-wording - shadow) mean
        drop exceeds `tolerance` (a material drop on the blind set = overfit);
      - leniency_flag (Pitfall 1): True if EVERY on-wording dimension mean is
        >= ~0.99 AND every shadow delta is ~0.0 (all-1.0 / no spread) — the judge
        may be rubber-stamping; escalate it rather than silently pass.
    """
    comparison = []
    overfit = False
    deltas = []
    on_means = []
    for needle, label in _SHARED_DIMS:
        on = _find_mean(on_metrics, needle)
        sh = _find_mean(sh_metrics, needle)
        if on is None or sh is None:
            comparison.append({
                "dimension": label, "onwording": on, "shadow": sh,
                "delta": None,
                "verdict": "n/a (dimension absent from one run)",
            })
            continue
        delta = round(on - sh, 4)
        deltas.append(delta)
        on_means.append(on)
        row_overfit = delta > tolerance
        if row_overfit:
            overfit = True
        comparison.append({
            "dimension": label,
            "onwording": round(on, 4),
            "shadow": round(sh, 4),
            "delta": delta,
            "verdict": ("OVERFIT (drop > tol)" if row_overfit
                        else "ok (within tolerance)"),
        })

    # Judge-leniency guard (Pitfall 1): all on-wording dims ~1.0 AND no spread.
    leniency = bool(on_means) and all(m >= 0.99 for m in on_means) \
        and bool(deltas) and all(abs(d) <= 0.01 for d in deltas)
    return comparison, overfit, leniency


def _fmt(v):
    return "n/a" if v is None else f"{v:.3f}" if isinstance(v, float) else str(v)


def write_results_report(host, endpoint, on_metrics, sh_metrics, comparison,
                         overfit, leniency):
    """Write 07-EVAL-RESULTS.md (mirrors test_supervisor.write_report style).

    CONFIDENTIALITY (T-07-01): this report carries METRICS + conceptual VERDICTS
    only — never verbatim Prompts.md answer-key sentences, and never held-out real
    ticket numbers / people. The dataset is already de-numbered (eval/dataset.py);
    this writer emits only numeric means, deltas, and capability-level verdicts.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    corr = _find_mean(on_metrics, "correctness")
    rel = _find_mean(on_metrics, "relevance")
    cite = _find_mean(on_metrics, "citation_groundedness")
    plaus = _find_mean(on_metrics, "plausible_reasoning")

    verdict = "OVERFIT" if overfit else "NOT OVERFIT"

    lines = [
        "# 07-EVAL-RESULTS — Agent Evaluation: Dimensions + Shadow Overfit Check "
        "(Plan 07-03)",
        "",
        f"**Generated:** {ts}",
        f"**Workspace:** `{host}`",
        f"**MAS endpoint:** `{endpoint}` (reused warm — READY; NOT re-provisioned, "
        "per 05-01 carry-forward)",
        f"**Experiment:** `{EXPERIMENT}` (named runs: `onwording`, `shadow`)",
        "**Harness:** `eval/run_eval.py --shadow` (re-runnable; "
        f"`MLFLOW_GENAI_EVAL_MAX_WORKERS=1` so all rows score — 07-02 OAuth-race "
        "mitigation).",
        "",
        "Scores are LLM-judge means (managed `databricks` judge). This report "
        "carries metrics + capability-level verdicts ONLY — no verbatim answer-key "
        "text and no held-out real entities (T-07-01).",
        "",
        "## On-Wording Run — Three Separate Dimension Metrics",
        "",
        "The five `Prompts.md` archetypes, scored on three DISTINCT dimensions "
        "(never one blended score) plus a plausible-reasoning guideline:",
        "",
        "| Dimension | Scorer | On-wording mean |",
        "|-----------|--------|-----------------|",
        f"| Correctness (claim-level vs expected_facts) | `Correctness` | "
        f"{_fmt(corr)} |",
        f"| Relevance to query | `RelevanceToQuery` | {_fmt(rel)} |",
        f"| Citation groundedness (corpus-resolving) | custom "
        f"`citation_groundedness` | {_fmt(cite)} |",
        f"| Plausible reasoning / hedge | `Guidelines` | {_fmt(plaus)} |",
        "",
        "*Correctness is graded against CONCEPTUAL / SHAPE `expected_facts` "
        "(de-numbered) — not verbatim answer-key text (T-07-01 leakage guard).*",
        "",
        "## Shadow Run — Blind Holdout (Ground-Truth-Free)",
        "",
        "The 15 blind paraphrases from Phase 3 "
        "(`03-SHADOW-PROMPTS.md`, 3 per archetype) — authored AFTER the corpus was "
        "generated and NEVER used to generate any ticket. They are **RUN, not "
        "regenerated** (regenerating with the answer key visible would contaminate "
        "the holdout — T-07-05). Correctness is EXCLUDED (shadows have no ground "
        "truth — it would error); only the ground-truth-free scorers run.",
        "",
        "| Dimension | Scorer | Shadow mean |",
        "|-----------|--------|-------------|",
        f"| Relevance to query | `RelevanceToQuery` | "
        f"{_fmt(_find_mean(sh_metrics, 'relevance'))} |",
        f"| Citation groundedness | custom `citation_groundedness` | "
        f"{_fmt(_find_mean(sh_metrics, 'citation_groundedness'))} |",
        f"| Plausible reasoning / hedge | `Guidelines` | "
        f"{_fmt(_find_mean(sh_metrics, 'plausible_reasoning'))} |",
        "",
        "## Overfit Comparison (On-Wording vs Shadow)",
        "",
        f"For each ground-truth-free dimension shared by both runs, "
        f"`delta = on-wording mean − shadow mean`. A drop larger than the "
        f"tolerance (**{OVERFIT_TOLERANCE}**) on the blind set is an OVERFIT "
        "signal — the agent would be leaning on the exact `Prompts.md` wording "
        "rather than generalizing.",
        "",
        "| Dimension | On-wording | Shadow | Delta (on − shadow) | Verdict |",
        "|-----------|-----------|--------|---------------------|---------|",
    ]
    for row in comparison:
        lines.append(
            f"| {row['dimension']} | {_fmt(row['onwording'])} | "
            f"{_fmt(row['shadow'])} | {_fmt(row['delta'])} | {row['verdict']} |")
    lines += [
        "",
        f"**Overfit verdict: {verdict}** "
        + ("— a material score drop on the blind holdout exceeds the tolerance on "
           "at least one dimension; investigate before trusting the on-wording "
           "scores."
           if overfit else
           "— no dimension drops more than the tolerance on the blind holdout; the "
           "agent generalizes beyond the exact `Prompts.md` wording."),
        "",
        "## Judge-Leniency Guard (Pitfall 1)",
        "",
    ]
    if leniency:
        lines += [
            "**LENIENCY WARNING.** Every on-wording dimension scored ~1.0 with no "
            "on-wording-vs-shadow spread. An all-1.0 / no-spread pattern is the "
            "classic signature of a lenient judge rubber-stamping answers rather "
            "than discriminating quality. **Recommended action:** escalate the "
            "judge model to a stronger reasoning model and re-run before trusting "
            "these scores — do NOT read this as a clean pass.",
        ]
    else:
        lines += [
            "**No leniency flag.** The dimension means show honest spread (not all "
            "~1.0) and/or a non-zero on-wording-vs-shadow delta, so the judge is "
            "discriminating answer quality rather than rubber-stamping. The guard "
            "would fire (recommending a stronger judge + re-run) only on an "
            "all-1.0 / no-spread pattern.",
        ]
    lines += [
        "",
        "## Known Limitation",
        "",
        "Full **MemAlign** SME judge alignment (a domain-expert labeling loop to "
        "calibrate the LLM judge) is **out of scope** for this demo — the MLflow "
        "Review App is excluded per PROJECT.md (RESEARCH Pitfall 1d). The "
        "leniency guard + the blind-shadow overfit cross-check are the pragmatic "
        "substitutes: they catch a rubber-stamping judge and wording-overfit "
        "without a human labeling round.",
        "",
    ]
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
