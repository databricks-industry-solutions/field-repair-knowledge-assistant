# Databricks notebook source
# MAGIC %md
# MAGIC # Gold enrichment — incremental `ai_query`
# MAGIC
# MAGIC Rebuilds `rd_tasks_gold_enrichment` from `rd_tasks_silver`. Runs as a serverless
# MAGIC **notebook** job task (`resources/jobs_pipeline.yml`), so it executes SQL through the
# MAGIC notebook's own Spark session (`spark.sql`) — no SQL warehouse, no statement-execution
# MAGIC polling.
# MAGIC
# MAGIC **Incremental** (only new/changed tickets reach the LLM): a `content_hash` LEFT ANTI
# MAGIC JOIN selects the `todo` set, `ai_query` runs over just those rows into a materialized
# MAGIC stage table, then a `MERGE` upserts by `number`. A re-run with no new tickets selects
# MAGIC zero rows, so `ai_query` makes zero model calls and the MERGE is skipped ($0).
# MAGIC
# MAGIC The enrichment schema, system prompt, and enum vocab are the single-source
# MAGIC `enrich_recipe.py` (co-located, imported below). Systems/vendors enums come from the
# MAGIC APPROVED glossary at run time — no hardcoded domain lists.

# COMMAND ----------

# Parameters — the DAB notebook_task passes catalog/schema via base_parameters.
dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
if not catalog or not schema:
    raise ValueError("catalog and schema widgets are required (set via notebook_task base_parameters)")
fq = f"{catalog}.{schema}"
print(f"[enrich] target: {fq}")

# COMMAND ----------

# Import the single-source recipe (co-located enrich_recipe.py, a workspace file).
# A workspace notebook's own folder is normally on sys.path; the fallback adds it
# explicitly from the notebook context path in case serverless does not.
import json
import os
import sys

try:
    import enrich_recipe as R
except ModuleNotFoundError:
    _nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    _dir = os.path.dirname(_nb if _nb.startswith("/Workspace") else "/Workspace" + _nb)
    sys.path.insert(0, _dir)
    import enrich_recipe as R

# COMMAND ----------

T_SILVER = f"{fq}.rd_tasks_silver"
T_GLOSSARY = f"{fq}.glossary"
T_GOLD = f"{fq}.rd_tasks_gold_enrichment"
T_STAGE = f"{fq}.rd_tasks_gold_enrich_stage"  # materialized so ai_query runs exactly once

# COMMAND ----------

# Step 1 — ensure the gold table exists so the first-run anti-join has a right side.
# content_hash is load-bearing: the incremental gate joins silver.content_hash to it.
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {T_GOLD} (
  number STRING, content_hash STRING,
  systems_involved ARRAY<STRING>, hardware_mentioned ARRAY<STRING>, vendors ARRAY<STRING>,
  problem_category STRING,
  summary STRING, customer_impact STRING, troubleshooting STRING, recommendation STRING,
  root_cause STRING, resolution STRING, resolution_type STRING,
  conf_systems DOUBLE, conf_root_cause DOUBLE, conf_resolution_type DOUBLE,
  min_confidence DOUBLE, needs_review BOOLEAN,
  prompt_version STRING, model STRING, enriched_at TIMESTAMP)
COMMENT 'LLM enrichment of R&D tasks via ai_query, gated by content_hash. Joins silver on number.'
""")

# COMMAND ----------

# Step 2 — build systems/vendors enums + acronym hints FROM the approved glossary.
rows = [
    (r["term"], r["category"], r["definition"])
    for r in spark.sql(
        f"SELECT term, category, definition FROM {T_GLOSSARY} WHERE status='approved'"
    ).collect()
]
systems, vendors, acr = R.parse_vocab(rows)
print(f"[enrich] vocab: {len(systems)} systems {systems}, {len(vendors)} vendors {vendors}, "
      f"{len(acr)} acronyms")
if not systems:
    raise ValueError("no approved category=system terms — cannot build the enum.")

# COMMAND ----------

# Step 3 — ai_query enrichment over silver LEFT ANTI JOIN gold, materialized into a stage table.
# Reference a single ai_query column ONCE (never twice) — hence a persisted stage, not a view.
rf_json = json.dumps(R.build_response_format(systems, vendors)).replace("'", "\\'")
sys_prompt_sql = R.build_system_prompt(systems, vendors, acr).replace("'", "\\'")

stage_sql = f"""
CREATE OR REPLACE TABLE {T_STAGE} AS
WITH todo AS (
  SELECT s.number, s.content_hash, s.title, s.description, s.notes, s.close_notes
  FROM {T_SILVER} s
  LEFT ANTI JOIN {T_GOLD} g ON s.content_hash = g.content_hash
),
scored AS (
  SELECT number, content_hash,
    ai_query(
      '{R.CHAT_ENDPOINT}',
      concat(
        '{sys_prompt_sql}',
        '\\n\\nTASK ', number, ': ', coalesce(title,''),
        '\\n\\nDESCRIPTION:\\n', coalesce(description,'(none)'),
        '\\n\\nNOTES:\\n', coalesce(notes,'(none)'),
        '\\n\\nCLOSE NOTES:\\n', coalesce(close_notes,'(none)')
      ),
      responseFormat => '{rf_json}'
    ) AS js
  FROM todo
),
parsed AS (
  SELECT number, content_hash, from_json(js, '{R.PARSE_STRUCT}') AS e
  FROM scored
),
-- Normalize confidence to 0..1: the model sometimes returns a 0..100 scale.
normed AS (
  SELECT number, content_hash, e,
    CASE WHEN e.conf_systems > 1 THEN e.conf_systems/100 ELSE e.conf_systems END AS c_sys,
    CASE WHEN e.conf_root_cause > 1 THEN e.conf_root_cause/100 ELSE e.conf_root_cause END AS c_rc,
    CASE WHEN e.conf_resolution_type > 1 THEN e.conf_resolution_type/100 ELSE e.conf_resolution_type END AS c_rt
  FROM parsed
)
SELECT number, content_hash,
  e.systems_involved, e.hardware_mentioned, e.vendors, e.problem_category,
  e.summary, e.customer_impact, e.troubleshooting, e.recommendation,
  e.root_cause, e.resolution, e.resolution_type,
  c_sys AS conf_systems, c_rc AS conf_root_cause, c_rt AS conf_resolution_type,
  least(c_sys, c_rc, c_rt)                        AS min_confidence,
  (least(c_sys, c_rc, c_rt) < {R.CONF_THRESHOLD}) AS needs_review,
  '{R.PROMPT_VERSION}'                            AS prompt_version,
  '{R.CHAT_ENDPOINT}'                             AS model,
  current_timestamp()                             AS enriched_at
FROM normed
"""
spark.sql(stage_sql)

# COMMAND ----------

# Step 4 — MERGE the newly enriched rows (idempotent, content_hash-gated). Skip on 0 rows.
n = spark.table(T_STAGE).count()
print(f"[enrich] enriched {n} new/changed task(s)")
if n:
    spark.sql(f"""
    MERGE INTO {T_GOLD} t USING {T_STAGE} s ON t.number = s.number
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """)
    print("[enrich] MERGE applied")
else:
    print("[enrich] MERGE skipped (0 new/changed tasks — incremental gate held, no ai_query cost)")

# COMMAND ----------

# Step 5 — drop the stage table (transient).
spark.sql(f"DROP TABLE IF EXISTS {T_STAGE}")
total = spark.table(T_GOLD).count()
print(f"[enrich] done — rd_tasks_gold_enrichment has {total} row(s)")
