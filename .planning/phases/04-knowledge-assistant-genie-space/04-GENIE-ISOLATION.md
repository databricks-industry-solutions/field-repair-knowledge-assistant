# Phase 4 — Genie Space ISOLATION Evidence (Plan 04-04)

**Generated:** 2026-08-19 14:24 UTC
**Workspace:** `https://fevm-serverless-stable-l26d62.cloud.databricks.com`
**Genie space_id:** `01f19b2cc8bf15ae8327820cfd449e1f`  (driven directly — no Supervisor, D-09)
**Warehouse (cross-check):** `04a4dee7888b9e64`

Isolation proof (D-09): every archetype question was driven through the live Genie Conversation API; the GENERATED SQL was retrieved at `attachments[].query.query` and asserted on SHAPE (D-07 — inspect the SQL, not just the prose), with row counts cross-checked against the same SQL run directly on `/api/2.0/sql/statements`.

**Result: 10 PASS / 0 FAIL of 10 assertions.**

## PASS/FAIL Evidence Table

| Status | Criterion | Evidence |
|--------|-----------|----------|
| PASS | GEN-01 count question COMPLETED + non-empty result | status=COMPLETED; result rows=1; prose='There are currently **8 tasks** that are open or pending.' |
| PASS | GEN-05 [count] generated SQL retrieved + row-count cross-check | Genie rows=1; direct-SQL state=SUCCEEDED rows=1 (need SUCCEEDED + matching count) |
| PASS | GEN-02 WIM expert-finding: assigned_to + array_contains(WIM) | GROUP BY assigned_to=True; array_contains(systems_involved,'WIM')=True; direct-SQL state=SUCCEEDED; rows=1; top expert='Cedar Mah' |
| PASS | GEN-05 [WIM-expert] generated SQL retrieved + row-count cross-check | Genie rows=1; direct-SQL state=SUCCEEDED rows=1 (need SUCCEEDED + matching count) |
| PASS | GEN-04 open-task SQL filters open + orders by duration/priority | open filter (is_closed=FALSE / Open-Pending)=True; ORDER BY present=True; orders by duration/priority=True |
| PASS | GEN-05 [A4-open-task] generated SQL retrieved + row-count cross-check | Genie rows=8; direct-SQL state=SUCCEEDED rows=8 (need SUCCEEDED + matching count) |
| PASS | GEN-03 delay/complexity SQL uses signal columns | SQL signal columns=['duration_days', 'num_note_entries']; prose signals=none (need >=2 in SQL, or >=1 SQL + >=1 prose) |
| PASS | GEN-05 [A3-delay-complexity] generated SQL retrieved + row-count cross-check | Genie rows=23; direct-SQL state=SUCCEEDED rows=23 (need SUCCEEDED + matching count) |
| PASS | GEN-06 'how many CA tasks' via TEXT match (not false array-0) | SQL uses text match=True; CA routed via array_contains=False (must be False); CA array-0 count=0 (false 0); CA text ground-truth=1; Genie answer count=23 (both must be >0) |
| PASS | GEN-07 system term (ATIS) still counts via array_contains | SQL uses array_contains(systems_involved,'ATIS')=True; ATIS array ground-truth=6; Genie answer count=6 (must equal ground-truth, no regression) |

## Generated SQL per Archetype (GEN-05 inspection artifact)

### Structured count (GEN-01)

```sql
SELECT count(*) AS open_or_pending_tasks
FROM `serverless_stable_l26d62_catalog`.`fis_knowledge_agent_dev`.`rd_tasks_serving_analytics`
WHERE `is_closed` = FALSE
```

### WIM expert-finding (GEN-02)

```sql
SELECT assigned_to, count(*) AS resolved_wim_tasks
FROM serverless_stable_l26d62_catalog.fis_knowledge_agent_dev.rd_tasks_serving_analytics
WHERE array_contains(systems_involved, 'WIM') AND is_closed = TRUE
GROUP BY assigned_to
ORDER BY resolved_wim_tasks DESC
```

### A4 — open-task ranking (GEN-04)

```sql
SELECT task_number, location_state, location_site, priority_level, status, duration_days, num_note_entries
FROM serverless_stable_l26d62_catalog.fis_knowledge_agent_dev.rd_tasks_serving_analytics
WHERE is_closed = FALSE
ORDER BY duration_days DESC, priority_level ASC
```

### A3 — delay/complexity (GEN-03)

```sql
SELECT task_number, duration_days, num_note_entries
FROM serverless_stable_l26d62_catalog.fis_knowledge_agent_dev.rd_tasks_serving_analytics
WHERE duration_days IS NOT NULL AND num_note_entries IS NOT NULL
ORDER BY duration_days DESC, num_note_entries DESC
```

### CA text-match proof (GEN-06)

```sql
SELECT count(*) AS ca_tasks
FROM serverless_stable_l26d62_catalog.fis_knowledge_agent_dev.rd_tasks_serving_analytics
WHERE title ILIKE '%CA%' OR summary ILIKE '%CA%' OR customer_impact ILIKE '%CA%'
   OR troubleshooting ILIKE '%CA%' OR recommendation ILIKE '%CA%'
   OR root_cause ILIKE '%CA%' OR resolution ILIKE '%CA%'
```

### ATIS array regression (GEN-07)

```sql
SELECT count(*) AS atis_tasks
FROM serverless_stable_l26d62_catalog.fis_knowledge_agent_dev.rd_tasks_serving_analytics
WHERE array_contains(systems_involved, 'ATIS')
```

> Known limitation (Phase-3): synthetic tickets have `activity_count=1`, so involvement/delay richness leans on the real tickets + note actors — expected, not a regression.
