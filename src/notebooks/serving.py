# Databricks notebook source
# MAGIC %md
# MAGIC # Serving table — the one surface BOTH engines read
# MAGIC
# MAGIC Rebuilds `rd_tasks_serving` as a **plain Delta table** (Change Data Feed on) so the
# MAGIC Knowledge Assistant can *stream* from it — a materialized view cannot be streamed
# MAGIC (`STREAMING_FROM_MATERIALIZED_VIEW`), which is why this is a job notebook task and not
# MAGIC a Lakeflow pipeline dataset.
# MAGIC
# MAGIC The table is `silver ⋈ rnd_tickets (metadata struct) ⋈ gold_enrichment ⋈ note counts`,
# MAGIC with `ka_content` (the single KA-indexed column) composed from the meaning-segmented
# MAGIC enrichment columns and glossary-acronym-expanded (single-source `enrich_recipe.py`).
# MAGIC Genie reads the curated `rd_tasks_serving_analytics` view (column COMMENTs = text-to-SQL
# MAGIC hints); `rd_tasks_gold_analytics` is a compat alias. `verify()` then asserts the KA
# MAGIC attach contract, including that the table is a real TABLE (not a view/MV).

# COMMAND ----------

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
if not catalog or not schema:
    raise ValueError("catalog and schema widgets are required (set via notebook_task base_parameters)")
fq = f"{catalog}.{schema}"
print(f"[serving] target: {fq}")

# COMMAND ----------

import os
import sys

try:
    import enrich_recipe as R
except ModuleNotFoundError:
    _nb = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    _dir = os.path.dirname(_nb if _nb.startswith("/Workspace") else "/Workspace" + _nb)
    sys.path.insert(0, _dir)
    import enrich_recipe as R

T_SILVER = f"{fq}.rd_tasks_silver"
T_TICKETS = f"{fq}.rnd_tickets"           # carries the required `metadata` struct
T_GOLD = f"{fq}.rd_tasks_gold_enrichment"
T_NOTE_ENTRIES = f"{fq}.rd_task_note_entries"
T_GLOSSARY = f"{fq}.glossary"
T_SERVING = f"{fq}.rd_tasks_serving"
KA_CONTENT_COL = "ka_content"

# COMMAND ----------

# Acronym map from the approved glossary — drives ka_content expansion.
acr_map = R.parse_acronym_map([
    (r["term"], r["definition"])
    for r in spark.sql(f"SELECT term, definition FROM {T_GLOSSARY} WHERE status='approved'").collect()
])
print(f"[serving] {len(acr_map)} approved acronyms for ka_content expansion")
if not acr_map:
    raise ValueError("no approved acronyms — refusing to build unexpanded KA content.")

# COMMAND ----------

# The SELECT that composes one serving row: silver + rnd_tickets(metadata) + enrichment + note counts.
# ka_content = the four meaning-segmented columns, glossary-acronym-expanded, newline-joined.
def serving_select():
    parts = [R.expand_expr(f"coalesce(e.{c}, '')", acr_map) for c in R.SEGMENT_COLS]
    ka_content = "concat_ws('\\n', " + ", ".join(parts) + ")"
    return f"""
SELECT
  -- identity
  s.number,
  s.content_hash,
  -- structured (Genie): straight from silver
  s.title, s.parent, s.assignment_group, s.assigned_to,
  s.priority_level, s.priority_label, s.status, s.workflow_status, s.is_closed,
  s.location, s.location_state, s.location_highway, s.location_direction,
  s.location_site, s.site_key,
  s.duration_days, s.num_activities, s.comment_count, s.max_inactivity_gap_days,
  coalesce(ne.num_note_entries, 0) AS num_note_entries,
  -- raw narrative (kept queryable)
  s.description, s.notes, s.close_notes, t.case_text,
  -- derived (enrichment)
  e.systems_involved, e.hardware_mentioned, e.vendors, e.problem_category,
  e.summary, e.customer_impact, e.troubleshooting, e.recommendation,
  e.root_cause, e.resolution, e.resolution_type,
  e.conf_systems, e.conf_root_cause, e.conf_resolution_type,
  e.min_confidence, e.needs_review,
  e.prompt_version, e.model, e.enriched_at,
  -- KA: the ONE indexed content column, pre-composed (file_col takes exactly one)
  {ka_content} AS {KA_CONTENT_COL},
  -- KA: REQUIRED struct — without it the attach fails outright
  t.metadata
FROM {T_SILVER} s
LEFT JOIN {T_TICKETS} t ON s.number = t.number
LEFT JOIN {T_GOLD} e ON s.number = e.number
LEFT JOIN (SELECT number, count(*) AS num_note_entries
           FROM {T_NOTE_ENTRIES} GROUP BY number) ne ON s.number = ne.number
""".strip()

# COMMAND ----------

# Build the streamable serving TABLE. CREATE OR REPLACE TABLE (never a view/MV) guarantees a
# plain, streamable managed Delta table even if a prior deploy left an MV of this name behind.
spark.sql(f"""
CREATE OR REPLACE TABLE {T_SERVING}
COMMENT 'One serving row per R&D task for BOTH engines. KA indexes ka_content (segmented + glossary-acronym-expanded) and cites via the metadata struct; Genie reads the structured columns. CDF on for the KA streaming attach.'
TBLPROPERTIES (delta.enableChangeDataFeed = true)
AS
{serving_select()}
""")
print(f"[serving] built {T_SERVING} (plain Delta, CDF on)")

# COMMAND ----------

# Curated Genie view + the rd_tasks_gold_analytics compat alias (same COMMENTed surface).
# Column COMMENTs are Genie's text-to-SQL hints; WITH SCHEMA COMPENSATION keeps them exact.
def sql_analytics_view(view):
    return f"""
CREATE OR REPLACE VIEW {fq}.{view} (
  task_number COMMENT 'R&D task id, e.g. R&DTASK0001070. Unique key.',
  title COMMENT 'Short description of the task/issue.',
  parent_case COMMENT 'Parent support case id (SDC...).',
  assigned_to COMMENT 'R&D team member who owns the task (domain-expert signal).',
  priority_level COMMENT '1=Critical..4=Low. Lower number = higher priority.',
  priority_label COMMENT 'Text priority: Critical/High/Moderate/Low.',
  status COMMENT 'Open, Pending, Closed Complete, Closed Skipped, Closed Incomplete.',
  workflow_status COMMENT 'Draft, Assigned, Work in progress, Completed.',
  is_closed COMMENT 'TRUE if status starts with Closed.',
  location_state COMMENT '2-letter US state / Canadian province of the site.',
  location_highway COMMENT 'Highway/route of the site, e.g. I-40, US-60.',
  location_site COMMENT 'Site name within the state, e.g. the town or interchange.',
  site_key COMMENT 'Canonical state:site key for grouping tasks by site.',
  duration_days COMMENT 'Days between first and last activity. Higher = longer to resolve.',
  num_note_entries COMMENT 'Count of dated note entries; proxy for back-and-forth/difficulty.',
  num_activities COMMENT 'Count of audit-trail activity events.',
  systems_involved COMMENT 'Array of screening systems: ALPR, ATIS, WIM, HTS, OVC, etc. Use array_contains().',
  hardware_mentioned COMMENT 'Array of hardware/components mentioned.',
  vendors COMMENT 'Array of vendors: Lumex, Veridyne, Aptix, etc.',
  problem_category COMMENT 'hardware_failure, software_crash, network_connectivity, calibration, image_quality, power, configuration, other.',
  summary COMMENT 'LLM-segmented: what the issue is (from the description).',
  customer_impact COMMENT 'LLM-segmented: effect on the customer/site. Empty if the ticket does not state one.',
  troubleshooting COMMENT 'LLM-segmented: steps already taken to verify or resolve.',
  recommendation COMMENT 'LLM-segmented: proposed fix / parts required.',
  root_cause COMMENT 'One-sentence LLM-extracted root cause, or undetermined.',
  resolution COMMENT 'What resolved the issue, or unresolved.',
  resolution_type COMMENT 'hardware_replace, software_patch, recalibration, config_change, rma, firmware_update, monitoring, no_fix_found, unresolved, not_applicable.',
  needs_review COMMENT 'TRUE if enrichment confidence was low (SME should verify).')
COMMENT 'Curated R&D task analytics for roadside truck-screening (WIM/ALPR/AUR/ATIS). One row per task over rd_tasks_serving — the SAME physical rows the Knowledge Assistant retrieves from. Use for counts, durations, expert-finding, priority and site-pattern analysis.'
WITH SCHEMA COMPENSATION
AS SELECT number, title, parent, assigned_to, priority_level, priority_label, status,
  workflow_status, is_closed, location_state, location_highway, location_site, site_key,
  duration_days, num_note_entries, num_activities,
  systems_involved, hardware_mentioned, vendors, problem_category,
  summary, customer_impact, troubleshooting, recommendation,
  root_cause, resolution, resolution_type, needs_review
FROM {T_SERVING}
""".strip()

spark.sql(sql_analytics_view("rd_tasks_serving_analytics"))
spark.sql(sql_analytics_view("rd_tasks_gold_analytics"))  # compat alias (tests/grants use it)
print("[serving] analytics views created")

# COMMAND ----------

# verify() — the KA attach contract + Genie read surface, plus the NEW table-TYPE check
# (a materialized view passes the CDF check but breaks the KA streaming attach — the gap
# that shipped the MV regression).
def _scalar(stmt):
    rows = spark.sql(stmt).collect()
    return rows[0][0] if rows else None

checks = []

n = _scalar(f"SELECT count(*) FROM {T_SERVING}")
checks.append(("row count > 0 (corpus non-empty)", n is not None and int(n) > 0, str(n)))

# NEW: rd_tasks_serving must be a real TABLE, not a view / materialized view.
ttype = _scalar(
    f"SELECT table_type FROM {catalog}.information_schema.tables "
    f"WHERE table_schema='{schema}' AND table_name='rd_tasks_serving'")
is_table = str(ttype).upper() in ("MANAGED", "EXTERNAL", "BASE TABLE", "MANAGED_TABLE")
checks.append(("rd_tasks_serving is a TABLE, not a view/MV (KA streaming attach)",
               is_table, str(ttype)))

names = [r["col_name"] for r in spark.sql(f"DESCRIBE {T_SERVING}").collect()
         if r["col_name"] and not r["col_name"].startswith("#")]
checks.append(("metadata struct present (KA attach requires it)", "metadata" in names,
               "present" if "metadata" in names else "MISSING"))
checks.append((f"{KA_CONTENT_COL} present (the one indexed column)", KA_CONTENT_COL in names,
               "present" if KA_CONTENT_COL in names else "MISSING"))

props = {r["key"]: r["value"] for r in spark.sql(f"SHOW TBLPROPERTIES {T_SERVING}").collect()}
cdf = str(props.get("delta.enableChangeDataFeed", "")).lower() in ("true", "supported")
checks.append(("CDF enabled (KA attach requires it)", cdf, "on" if cdf else "OFF"))

n = _scalar(f"SELECT count(*) FROM {T_SERVING} WHERE {KA_CONTENT_COL} IS NULL OR length({KA_CONTENT_COL}) = 0")
checks.append(("no null/empty ka_content == 0", n is not None and int(n) == 0, str(n)))

n = _scalar(f"SELECT count(*) FROM {T_SERVING} WHERE metadata.file_path IS NULL")
checks.append(("every row has a citation file_path == 0 null", n is not None and int(n) == 0, str(n)))

n = _scalar(f"SELECT count(*) FROM {T_SERVING} WHERE {KA_CONTENT_COL} LIKE '%AUR (%'")
checks.append(("acronym expansion present (ka_content LIKE 'AUR (%')", n is not None and int(n) > 0, str(n)))

n = _scalar(f"SELECT count(*) - count(DISTINCT number) FROM {T_SERVING}")
checks.append(("number is unique (no join fan-out)", n is not None and int(n) == 0, str(n)))

n = _scalar(f"SELECT count(*) FROM {T_SERVING} WHERE problem_category IS NULL")
checks.append(("every row carries enrichment == 0 null category", n is not None and int(n) == 0, str(n)))

n = _scalar(f"SELECT count(*) FROM {fq}.rd_tasks_serving_analytics")
checks.append(("analytics view readable (rows > 0)", n is not None and int(n) > 0, str(n)))

ok = True
for label, passed, got in checks:
    print(f"  [{'PASS' if passed else 'FAIL'}] {label} (got {got})")
    ok = ok and passed
if not ok:
    raise ValueError("VERIFY FAILED — rd_tasks_serving does not satisfy the KA attach + Genie read contract.")
print("VERIFY PASSED — rd_tasks_serving is a streamable plain Delta table satisfying BOTH engines.")
