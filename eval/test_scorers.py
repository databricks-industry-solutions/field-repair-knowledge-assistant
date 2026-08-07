#!/usr/bin/env python3
"""FIS AI Knowledge Agent — Phase 7 Plan 02 Task 2 (TDD RED): scorer tests.

Exercises the two DETERMINISTIC citation_groundedness branches without hitting
the live judge:
  1. zero citations               -> Feedback value="no"
  2. a hallucinated / non-corpus citation -> Feedback value="no" naming it

The "supports-the-claim" LLM-judge branch (all cites resolve) is proven live in
run_eval (human-check), not stubbed here. We monkeypatch the reused
`tickets_in_corpus` + `fetch_case_text` so these tests are offline + fast.

Run:  python3 eval/test_scorers.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "eval"))

import scorers  # noqa: E402


def _run():
    results = []

    # 1. Zero-citation answer -> "no" (claims with no grounding).
    fb = scorers.score_citation_groundedness(
        inputs={"question": "why is WIM reading zero?"},
        outputs={"response": "It is probably a sensor fault.", "citations": []},
        profile="test",
    )
    t1 = fb.value == "no" and "zero" in (fb.rationale or "").lower()
    results.append(("zero-citation => no", t1, f"value={fb.value} rationale={fb.rationale!r}"))

    # 2. Hallucinated citation -> "no" naming the offending id.
    #    Stub tickets_in_corpus so NO cite resolves.
    orig = scorers.tickets_in_corpus
    scorers.tickets_in_corpus = lambda nums, profile: set()
    try:
        fb = scorers.score_citation_groundedness(
            inputs={"question": "prioritize open tasks"},
            outputs={"response": "See R&DTASK9999999.",
                     "citations": ["R&DTASK9999999"]},
            profile="test",
        )
    finally:
        scorers.tickets_in_corpus = orig
    t2 = fb.value == "no" and "9999999" in (fb.rationale or "")
    results.append(("hallucinated-cite => no (named)", t2,
                    f"value={fb.value} rationale={fb.rationale!r}"))

    # 3. All cites resolve -> defers to the judge (we stub fetch_case_text +
    #    the judge to prove the branch is reached, not the judge verdict).
    orig_tic = scorers.tickets_in_corpus
    orig_fetch = scorers.fetch_case_text
    orig_judge = scorers.meets_guidelines
    scorers.tickets_in_corpus = lambda nums, profile: {"R&DTASK0002001"}
    scorers.fetch_case_text = lambda nums, profile: "AUR camera offline; swapped unit."
    sentinel = {"called": False}

    def _fake_judge(**kwargs):
        sentinel["called"] = True
        from mlflow.entities import Feedback
        return Feedback(name="citation_groundedness", value="yes",
                        rationale="stub judge")
    scorers.meets_guidelines = _fake_judge
    try:
        fb = scorers.score_citation_groundedness(
            inputs={"question": "AUR expert?"},
            outputs={"response": "R&DTASK0002001 shows the fix.",
                     "citations": ["R&DTASK0002001"]},
            profile="test",
        )
    finally:
        scorers.tickets_in_corpus = orig_tic
        scorers.fetch_case_text = orig_fetch
        scorers.meets_guidelines = orig_judge
    t3 = sentinel["called"] and fb.value == "yes"
    results.append(("resolved-cites => judge invoked", t3,
                    f"judge_called={sentinel['called']} value={fb.value}"))

    print(f"\n{'STATUS':6}  TEST")
    print("-" * 60)
    n_fail = 0
    for name, ok, ev in results:
        print(f"{'PASS' if ok else 'FAIL':6}  {name}")
        print(f"          └─ {ev}")
        if not ok:
            n_fail += 1
    print("-" * 60)
    print(f"{len(results) - n_fail} PASS / {n_fail} FAIL")
    return n_fail


if __name__ == "__main__":
    sys.exit(1 if _run() else 0)
