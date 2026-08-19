#!/usr/bin/env python3
"""Field Repair Knowledge Assistant — Prompts.md loader (answer key).

`Prompts.md` is the client's PRIVATE acceptance bar — the 5 archetype questions
plus their expected-answer bullets. It is deliberately NOT tracked in the repo
(it references real-sample ticket numbers / people held OUT of the synthetic
corpus). This module reads it via `RKB_PROMPTS_PATH` (mirroring
`synth/leakage_gate.py`) INTO MEMORY ONLY.

HARD confidentiality rule: this loader must NEVER write any
Prompts.md content — neither the questions nor the answer bullets — to any file
on disk. It only returns an in-memory structure. Callers (eval/dataset.py) use it
to align archetype phrasing/count; they must NOT copy verbatim answer-key
sentences into any committed file, and must de-number all held-out real entities.

Prompts.md structure (mirrors leakage_gate's convention):
  '# <archetype title>'   -> an archetype section header
  `backtick query line`   -> a user question for that archetype (NOT an answer)
  '- ' bullet             -> an expected-answer bullet (the answer key)
"""

import os
from pathlib import Path

# The single private answer-key source. Read-only, in-memory only. NEVER committed
# (data/servicenow/Prompts.md is gitignored). Override the location with RKB_PROMPTS_PATH.
PROMPTS_PATH = Path(
    os.environ.get(
        "RKB_PROMPTS_PATH",
        str(Path(__file__).resolve().parents[1] / "data" / "servicenow" / "Prompts.md"),
    )
)

# Stable archetype ids assigned by header order (A1..A5), matching the
# test_supervisor MATRIX naming so the on-wording dataset aligns 1:1.
_ARCHETYPE_IDS = ["A1", "A2", "A3", "A4", "A5"]


def _is_query_line(stripped):
    """A full-line inline-code query: `...` (backtick-wrapped)."""
    return (
        len(stripped) >= 2
        and stripped.startswith("`")
        and stripped.endswith("`")
    )


def load_archetypes(prompts_path=PROMPTS_PATH):
    """Parse Prompts.md into an in-memory list of archetype dicts.

    Returns a list of `{id, title, questions: [...], answer_bullets: [...]}` in
    header order. IN-MEMORY ONLY — this function writes nothing to disk.

    Raises FileNotFoundError (naming RKB_PROMPTS_PATH) when the answer key is
    absent — there is deliberately NO fabricated fallback (a fabricated fallback
    would silently degrade the eval's ground truth).
    """
    prompts_path = Path(prompts_path)
    if not prompts_path.exists():
        raise FileNotFoundError(
            f"Prompts.md answer key not found at {prompts_path}. Set "
            "RKB_PROMPTS_PATH to the private answer-key file (it is intentionally "
            "not tracked in the repo). No fabricated fallback is used."
        )

    archetypes = []
    current = None
    for line in prompts_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---":
            continue
        if stripped.startswith("# "):
            current = {
                "id": None,
                "title": stripped[2:].strip(),
                "questions": [],
                "answer_bullets": [],
            }
            archetypes.append(current)
        elif current is None:
            continue
        elif _is_query_line(stripped):
            current["questions"].append(stripped.strip("`").strip())
        elif stripped.startswith("- "):
            current["answer_bullets"].append(stripped[2:].strip())

    # Assign stable A1..A5 ids by header order.
    for i, arc in enumerate(archetypes):
        arc["id"] = _ARCHETYPE_IDS[i] if i < len(_ARCHETYPE_IDS) else f"A{i + 1}"
    return archetypes


if __name__ == "__main__":
    # Diagnostic ONLY — prints COUNTS/ids, never the answer-key content itself
    # (so running this never leaks the private questions/bullets to a terminal log
    # in a way that could be captured to disk).
    arcs = load_archetypes()
    print(f"Loaded {len(arcs)} archetype(s) from {PROMPTS_PATH}:")
    for a in arcs:
        print(f"  {a['id']}: title-len={len(a['title'])} "
              f"questions={len(a['questions'])} "
              f"answer_bullets={len(a['answer_bullets'])}")
