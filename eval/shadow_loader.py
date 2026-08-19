#!/usr/bin/env python3
"""Field Repair Knowledge Assistant — blind shadow-prompt loader.

Loads the 15 pre-existing BLIND shadow prompts authored in Phase 3
(`reports/03-SHADOW-PROMPTS.md`) into
ground-truth-FREE MLflow eval rows. These are paraphrased variants of the 5
private-answer-key archetypes that the client did NOT write and that were
authored AFTER the synthetic corpus was built — the overfit / anti-leakage
holdout.

HARD INTEGRITY RULE: this loader READS the
pre-existing blind set only. It must NEVER:
  - call any paraphraser / model / synthesizer to re-author the questions,
  - read the private answer key (this module deliberately never imports the
    prompts_loader nor touches any answer-key path).
Re-authoring the paraphrases with the answer key visible would contaminate the
blind holdout and defeat the whole point of the overfit check. The 15 rows are
read verbatim from the committed 03-SHADOW-PROMPTS.md and carry NO `expectations`
key (they have no ground truth by design — Correctness cannot and must not run
on them).

Shadow-prompt file structure (verbatim, from Phase 3):
  '## A<n> — <theme>'   -> an archetype section header (A1..A5)
  '1. "<paraphrase>"'   -> a numbered, double-quote-wrapped question line
  (15 questions total, 3 per archetype.)
"""

import re
from pathlib import Path

# The ONE input: the committed blind holdout. Path relative to REPO_ROOT — this is
# a repo-tracked artifact, NOT the private answer key (which this module never
# reads).
REPO_ROOT = Path(__file__).resolve().parents[1]
SHADOW_PATH = (
    REPO_ROOT
    / "reports"
    / "03-SHADOW-PROMPTS.md"
)

# '## A3 — Task-complexity ...' — capture the archetype id from the header.
_HEADER_RE = re.compile(r"^##\s+(A\d+)\b")
# '1. "A few tickets mention ..."' — a numbered, double-quote-wrapped question.
_QUESTION_RE = re.compile(r'^\d+\.\s+"(.+)"\s*$')


def load_shadow(shadow_path=SHADOW_PATH):
    """Parse 03-SHADOW-PROMPTS.md into the blind shadow MLflow eval dataset.

    Returns a list of CLEAN MLflow rows of shape:
        {"inputs": {"question": <paraphrase>}, "archetype": "A<n>"}
    with NO `expectations` key (ground-truth-free by design — the shadow set was
    authored blind and has no answer bullets). The `archetype` tag drives the
    per-archetype breakdown in the report; MLflow ignores unknown top-level keys
    for scoring, and the ground-truth-free scorers only read `inputs`/`outputs`.

    Raises FileNotFoundError if the committed holdout is missing — there is NO
    fabricated / re-authored fallback.
    """
    shadow_path = Path(shadow_path)
    if not shadow_path.exists():
        raise FileNotFoundError(
            f"Blind shadow holdout not found at {shadow_path}. It is a committed "
            "Phase-3 artifact and must be RUN as-is, never re-authored (re-authoring "
            "with the answer key visible would contaminate the overfit holdout)."
        )

    rows = []
    current = None
    for line in shadow_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        h = _HEADER_RE.match(stripped)
        if h:
            current = h.group(1)
            continue
        if current is None:
            continue
        q = _QUESTION_RE.match(stripped)
        if q:
            rows.append({
                "inputs": {"question": q.group(1).strip()},
                "archetype": current,
            })
    return rows


if __name__ == "__main__":
    # Diagnostic — prints COUNTS/ids and a short prefix of each blind paraphrase
    # (these are NOT answer-key content; the shadow file is repo-tracked/public).
    ds = load_shadow()
    print(f"Loaded {len(ds)} blind shadow row(s) from {SHADOW_PATH}:")
    per = {}
    for r in ds:
        per[r["archetype"]] = per.get(r["archetype"], 0) + 1
    for aid in sorted(per):
        print(f"  {aid}: {per[aid]} question(s)")
    assert all("expectations" not in r for r in ds), \
        "shadow rows must be ground-truth-free (no expectations key)"
