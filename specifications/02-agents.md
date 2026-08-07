# Agents — Knowledge Assistant, Genie, Supervisor

## Shared Context

**Target:** `{{CATALOG}}.{{SCHEMA}}`. Both engines read `rd_tasks_serving` — the KA
its content column, Genie the structured columns via
`rd_tasks_serving_analytics`. Same physical rows, two access patterns.

**Not native to DAB.** `databricks bundle schema` has **no** resource type for a
Knowledge Assistant or a Supervisor: `knowledge_assistants`, `agent` and
`supervisor` appear zero times. `genie_spaces` **is** native. So the Genie space is
a bundle resource and the KA/Supervisor are built by idempotent scripts run as job
tasks. Consequence, stated plainly: no drift detection, and `bundle destroy` will
not remove them.

---

## A. Knowledge Assistant

`src/deploy/build_serving_agents.py`

Two knowledge sources:

| Source | Type | Points at |
|---|---|---|
| corpus | `file_table` | `rd_tasks_serving`, `file_col: ka_content` |
| glossary | `files` | the glossary Volume **directory** |

Four constraints, each verified against the live API rather than assumed:

1. **`file_col` takes exactly one column.** Two fails with `Array must have size 1,
   but has size 2`. So the content column must be pre-composed upstream.
2. **The `metadata` struct is required**, and checked *before* column validation —
   a table without it fails with `missing required column '_metadata'`.
3. **CDF or a streaming table is required.** This is what rules out a view.
4. **`file_col` is immutable.** Changing the indexed column requires DELETE +
   re-create of the knowledge source, which forces a full re-index. Plan the
   content column before first attach.

### Instructions are load-bearing

The KA's instructions are four numbered rules — CITE EVERYTHING / GIVE STEPS / ASK
FIRST IF UNCLEAR / HEDGE ON TERMS — plus 8 labeled examples.

This is not stylistic. Measured across the five archetypes on **byte-identical**
indexed content:

| Config | avg citations | numbered steps | Sources: line |
|---|---|---|---|
| paragraph-form instructions, no examples | 1.2 | 0/5 | 0/5 |
| the four numbered rules, no examples | 4.0 | 3/5 | 3/5 |
| numbered rules + 8 examples | **6.0** | 3/5 | 5/5 |

The instructions do most of the work; the examples close the gap on the
`Sources:` line. If a rebuild produces vague, uncited answers, check the
instructions before suspecting retrieval. `--verify` asserts instruction and
example parity for exactly this reason.

## B. Genie space

`genie/genie_space.json` — a bundle `genie_spaces` resource.

Carries the tuned text-to-SQL steering: column synonyms, 4 certified queries, and
one filter rule that must never be violated:

> `systems_involved`, `hardware_mentioned` and `vendors` are ARRAYS. A term is a
> `systems_involved` value **only** if its glossary category is `system` →
> `array_contains(...)`. For any other category the term is **not** in the arrays →
> match the TEXT columns with `ILIKE`. Never `array_contains` a non-system term; it
> returns a false 0.

Every column carries a `COMMENT` in the view — those comments *are* Genie's
text-to-SQL hints. `priority_level COMMENT '1=Critical..4=Low. Lower number =
higher priority.'` is the difference between triage sorting correctly and exactly
backwards.

Re-export after any console tuning, or a deploy reverts it. Note the query param —
a plain GET omits `serialized_space` entirely:

```bash
databricks api get \
  "/api/2.0/genie/spaces/<id>?include_serialized_space=true" -p <profile> \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['serialized_space'])" \
  > genie/genie_space.json
```

## C. Supervisor

`src/deploy/build_supervisor.py`

Registers three tools with sharp, non-overlapping descriptions:

| Tool | Handles |
|---|---|
| Knowledge Assistant | similar-case retrieval, recurring patterns, "has this happened before" |
| Genie space | counts, durations, expert-finding, priority triage, site patterns |
| `glossary_lookup` UC function | terminology disambiguation |

**Terminology resolves at the supervisor**, not inside either tool: resolve the
term → read its `category` → pass term *and* category downstream. That is what
makes the `CA` question correct, because Genie needs to know it is `software` to
choose `ILIKE` over `array_contains`.

Hybrid questions fan out to both tools and the answers are synthesized.

## D. The five query archetypes

The corpus and steering are shaped so all five work:

1. **Terminology** — "what does CA mean?" → glossary, hedged, corroborated
2. **Expert-finding** — "who is our WIM expert?" → Genie, most closed WIM tasks
3. **Complexity / delay** — "what took longest?" → `duration_days`, `num_note_entries`
4. **Priority triage** — "what should we work on?" → open tasks by priority + age
5. **Site patterns** — "what recurs in New Mexico?" → KA + Genie, grouped by site
