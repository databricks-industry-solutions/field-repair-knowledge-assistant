#!/usr/bin/env python3
"""Field Repair Knowledge Assistant — claim-decomposed on-wording dataset.

Builds the 5-archetype MLflow eval dataset (`mlflow.genai.evaluate` `data=`),
one row per `Prompts.md` archetype. Each row carries:

  inputs        = {"question": <leakage-free archetype question>}
  expectations  = {"expected_facts": [<CONCEPTUAL/SHAPE facts>],
                   "guidelines":     [<hedge / citation / plausible-reasoning rules>]}

CRITICAL de-numbering rule:
`Prompts.md` expected answers reference real-sample entities (specific ticket
numbers, a Mantis id, and named engineers) that were deliberately held OUT of the
synthetic corpus by the Phase-3 leakage gate. The agent physically cannot
reproduce them — it cites synthetic twins. Therefore `expected_facts` capture the
CAPABILITY / SHAPE of a good answer, NOT verbatim answer-key text or those
held-out entities (this file is grep-gated to contain none of them). Facts are
derived from:
  - the PUBLIC acronym glossary (src/deploy/glossary.md): CA -> Controller
    Application, HTS, WIM, AUR, OVC, PowerNode, ALPR — safe conceptual anchors;
  - the SHAPE assertions already proven in src/deploy/test_supervisor.py
    (check_sup04 three priority buckets; check_sup05 sw/hw split + direction).

We call `load_archetypes()` ONLY to confirm the archetype count/order aligns with
`Prompts.md` — the on-wording question TEXT reuses the leakage-free
test_supervisor MATRIX phrasing so NOTHING verbatim from the private answer key
lands in a committed file. No answer-key content is ever written to disk here.

Verify-gate: `grep` must find NO held-out real ticket numbers or person names in
this file (the de-numbering rule is enforced, not just intended).
"""

import sys
from pathlib import Path

# Align to the archetypes actually present in the private answer key (count/order
# only — never copy its text). If the answer key is absent, load_archetypes raises
# a clear RKB_PROMPTS_PATH error (no fabricated fallback).
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "eval"))
from prompts_loader import load_archetypes  # noqa: E402

# Leakage-free question phrasing, reused verbatim from the proven
# src/deploy/test_supervisor.py MATRIX (grounded on the live synthetic corpus, NOT on
# Prompts.md verbatim, no real-sample ticket numbers). One question per archetype.
_QUESTIONS = {
    # A1 — terminology (CA acronym resolution).
    "A1": (
        "What does the acronym CA mean in these R&D tickets, and is it a "
        "roadside screening system or a software/controller term?"
    ),
    # A2 — project-user correlation / expert-finding (AUR domain expert).
    "A2": (
        "Who is the go-to engineer for AUR camera issues, and which prior "
        "cases back that up?"
    ),
    # A3 — task-complexity / delay-cause identification.
    "A3": (
        "Which kinds of tasks take the longest to resolve, and why?"
    ),
    # A4 — priority sorting (marquee): three prioritization buckets.
    "A4": (
        "Among our currently open tasks, which should the team prioritize "
        "right now, and why?"
    ),
    # A5 — site-specific recurring patterns (New Mexico).
    "A5": (
        "What recurring problems keep coming back in New Mexico, and what "
        "should we do about them?"
    ),
}

# CONCEPTUAL expected_facts (SHAPE, de-numbered). Derived from the public glossary
# + the SHAPE assertions in test_supervisor.check_sup04/05 — NEVER verbatim
# answer-key sentences and NEVER held-out real entities.
_EXPECTED_FACTS = {
    "A1": [
        "CA most likely resolves to Controller Application",
        "CA is a software/controller-layer term (a component of the HTS "
        "controller stack), not a roadside vehicle-screening system",
        "The answer grounds the definition in more than one ticket / a glossary "
        "reference rather than a single ticket",
    ],
    "A2": [
        "The answer names a specific engineer as the go-to expert for AUR "
        "camera issues",
        "The answer supports that choice by citing prior similar cases (ticket "
        "pointers), not just asserting a name",
    ],
    "A3": [
        "The answer identifies which task categories run longest to resolve "
        "(e.g. camera replacements / aging-hardware work)",
        "The answer attributes the delay to a concrete cause such as long "
        "inactive gaps between activities or hardware/design dependencies",
    ],
    "A4": [
        "The answer produces three distinct prioritization buckets: an "
        "easiest-with-a-known-fix task, an oldest-but-blocked task, and a "
        "stubborn/recurring task where prior fixes failed",
        "Each bucket is backed by a concrete ticket pointer that resides in the "
        "corpus (not a hallucinated number)",
    ],
    "A5": [
        "The answer names at least two corpus-resident tickets for the site",
        "The answer separates a software root cause from a hardware root cause "
        "(e.g. a controller/web-app software issue vs. aging-hardware "
        "replacement)",
        "The answer prescribes a direction / recommended next step (e.g. a "
        "software patch vs. hardware upgrade)",
    ],
}

# Per-row guidelines carry the hedge + citation-required + plausible-reasoning
# allowance (consumed by the Guidelines "plausible_reasoning" scorer in run_eval,
# which references 'request'/'response' auto-extracted from the trace).
_GUIDELINES = {
    "A1": [
        "The response must HEDGE the acronym definition (e.g. 'most likely', "
        "'could refer to') rather than assert a single meaning flatly",
        "The response must cite grounding evidence (a glossary reference and/or "
        "corpus tickets) rather than answering from nowhere",
        "The reasoning must be plausible: it may reason from co-occurring "
        "tickets, but must not invent facts absent from the corpus",
    ],
    "A2": [
        "The response must cite prior similar cases (ticket pointers) to support "
        "the named expert",
        "The reasoning must be plausible and must not fabricate an engineer with "
        "no supporting cases",
    ],
    "A3": [
        "The response must ground the 'longest to resolve' claim in evidence "
        "(category patterns and/or cited tickets)",
        "The reasoning about delay causes must be plausible and tied to the "
        "cited evidence, not speculative",
    ],
    "A4": [
        "Every prioritized task must carry a corpus-resident ticket pointer",
        "The reasoning must be plausible: the easiest/oldest/stubborn framing "
        "must follow from status, age, and activity-history signals, not be "
        "asserted arbitrarily",
    ],
    "A5": [
        "The response must cite corpus-resident tickets for the site",
        "The software-vs-hardware split and the prescribed direction must be "
        "plausible and follow from the cited evidence",
    ],
}


ARCHETYPE_IDS = ["A1", "A2", "A3", "A4", "A5"]


def build_onwording(profile=None, only=None):
    """Return the claim-decomposed on-wording MLflow eval dataset.

    Each returned row is a CLEAN MLflow record — `{"inputs": {...},
    "expectations": {...}}` only (no extra keys that could confuse
    `mlflow.genai.evaluate`). `only` is an optional set/list of archetype ids
    (e.g. {"A1"}) to subset the dataset for a fast smoke run.

    `profile` is accepted for signature-compatibility with the run harness (an
    optional corpus SELECT to sharpen facts is out of scope — facts are kept
    conceptual per the de-numbering rule / Open Q1 resolution). No Prompts.md
    content is written to disk; `load_archetypes()` is used only to confirm the
    archetype count aligns with the private answer key.
    """
    # Alignment check: confirm the private answer key still has the 5 archetypes
    # this dataset is built against (count/order only — text never copied).
    arcs = load_archetypes()
    if len(arcs) < len(_QUESTIONS):
        raise ValueError(
            f"Prompts.md has {len(arcs)} archetype header(s) but the on-wording "
            f"dataset expects {len(_QUESTIONS)} ({sorted(_QUESTIONS)}). The "
            "answer-key structure changed — re-align eval/dataset.py."
        )

    want = {a.upper() for a in only} if only else None
    rows = []
    for aid in ARCHETYPE_IDS:
        if want is not None and aid not in want:
            continue
        rows.append({
            "inputs": {"question": _QUESTIONS[aid]},
            "expectations": {
                "expected_facts": list(_EXPECTED_FACTS[aid]),
                "guidelines": list(_GUIDELINES[aid]),
            },
        })
    return rows


if __name__ == "__main__":
    ds = build_onwording()
    print(f"Built {len(ds)} on-wording rows:")
    for r in ds:
        print(f"  facts={len(r['expectations']['expected_facts'])} "
              f"guidelines={len(r['expectations']['guidelines'])} "
              f"q='{r['inputs']['question'][:48]}...'")
