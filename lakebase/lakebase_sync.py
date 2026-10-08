# Databricks notebook source
# MAGIC %md
# MAGIC # Lakebase sync: Lakeflow silver -> Lakebase `fis_tasks`
# MAGIC
# MAGIC Closes the data path from ingestion to the app:
# MAGIC
# MAGIC `servicenow_landing` Volume -> Lakeflow `servicenow_rd_task_silver` -> **this notebook**
# MAGIC -> Lakebase `fis_tasks` (pgvector + full-text) -> v2 agent and app.
# MAGIC
# MAGIC Runs as a serverless notebook job task after the `servicenow_lakeflow` pipeline and
# MAGIC the `enrich` task (`resources/jobs_pipeline.yml`).
# MAGIC
# MAGIC **Incremental.** It reads the `content_hash` already stored in Lakebase and only
# MAGIC embeds and upserts tickets that are new or whose content changed. A re-run with no
# MAGIC changes makes zero embedding calls and zero writes.
# MAGIC
# MAGIC Why a notebook and not a synced table: the app's hybrid search needs a native
# MAGIC pgvector `vector(1024)` column plus a GIN full-text index, and the embedding is
# MAGIC computed here with `ai_query`. A synced table would mirror Delta types only.

# COMMAND ----------

# MAGIC %pip install -q "psycopg[binary]>=3.1" "databricks-sdk>=0.68"

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# Parameters: the DAB notebook_task passes these via base_parameters.
dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("lakebase_project", "fis")
dbutils.widgets.text("lakebase_branch", "production")
dbutils.widgets.text("lakebase_endpoint", "primary")
dbutils.widgets.text("lakebase_database", "databricks_postgres")
dbutils.widgets.text("lakebase_table", "fis_tasks")
dbutils.widgets.text("embedding_endpoint", "databricks-gte-large-en")

catalog = dbutils.widgets.get("catalog").strip()
schema = dbutils.widgets.get("schema").strip()
if not catalog or not schema:
    raise ValueError("catalog and schema widgets are required (set via notebook_task base_parameters)")
fq = f"{catalog}.{schema}"

LB_PROJECT = dbutils.widgets.get("lakebase_project").strip()
LB_BRANCH = dbutils.widgets.get("lakebase_branch").strip()
LB_ENDPOINT = dbutils.widgets.get("lakebase_endpoint").strip()
LB_DB = dbutils.widgets.get("lakebase_database").strip()
LB_TABLE = dbutils.widgets.get("lakebase_table").strip()
EMBED = dbutils.widgets.get("embedding_endpoint").strip()
EMBED_DIM = 1024  # databricks-gte-large-en

T_SN_SILVER = f"{fq}.servicenow_rd_task_silver"   # Lakeflow AUTO CDC output
T_SILVER = f"{fq}.rd_tasks_silver"                # parsed location fields
T_GOLD = f"{fq}.rd_tasks_gold_enrichment"         # ai_query enrichment
print(f"[lakebase_sync] {T_SN_SILVER} -> lakebase {LB_PROJECT}/{LB_BRANCH}.{LB_TABLE}")

# COMMAND ----------

# Lakebase connection: OAuth credential minted for whoever runs the job (user or SP).
import psycopg
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
ENDPOINT_NAME = f"projects/{LB_PROJECT}/branches/{LB_BRANCH}/endpoints/{LB_ENDPOINT}"
LB_HOST = w.postgres.get_endpoint(name=ENDPOINT_NAME).status.hosts.host
LB_USER = w.current_user.me().user_name


def connect():
    token = w.postgres.generate_database_credential(endpoint=ENDPOINT_NAME).token
    return psycopg.connect(host=LB_HOST, dbname=LB_DB, user=LB_USER, password=token, sslmode="require")

# COMMAND ----------

# Step 1: make sure the target table, its key, and both search indexes exist.
# Idempotent; safe against the table the v2 agent already reads.
DDL = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS {LB_TABLE} (
  number            TEXT PRIMARY KEY,
  title             TEXT,
  status            TEXT,
  priority_label    TEXT,
  location          TEXT,
  location_state    TEXT,
  location_site     TEXT,
  assigned_to       TEXT,
  opened_date       TIMESTAMP,
  closed_date       TIMESTAMP,
  problem_category  TEXT,
  root_cause        TEXT,
  resolution        TEXT,
  resolution_type   TEXT,
  lakebase_text     TEXT,
  lakebase_vector   vector({EMBED_DIM})
);
{{alters}}
CREATE UNIQUE INDEX IF NOT EXISTS idx_{LB_TABLE}_number ON {LB_TABLE} (number);
CREATE INDEX IF NOT EXISTS idx_{LB_TABLE}_text ON {LB_TABLE} USING GIN (to_tsvector('english', lakebase_text));
"""

# The v2 table may predate this notebook with a different column set; add anything
# missing rather than failing the upsert.
COLUMN_TYPES = {
    "title": "TEXT", "status": "TEXT", "priority_label": "TEXT", "location": "TEXT",
    "location_state": "TEXT", "location_site": "TEXT", "assigned_to": "TEXT",
    "opened_date": "TIMESTAMP", "closed_date": "TIMESTAMP", "problem_category": "TEXT",
    "root_cause": "TEXT", "resolution": "TEXT", "resolution_type": "TEXT",
    "lakebase_text": "TEXT", "lakebase_vector": f"vector({EMBED_DIM})",
    "content_hash": "TEXT", "synced_at": "TIMESTAMPTZ",
}
DDL = DDL.replace("{alters}", "\n".join(
    f"ALTER TABLE {LB_TABLE} ADD COLUMN IF NOT EXISTS {c} {t};" for c, t in COLUMN_TYPES.items()))

with connect() as conn:
    conn.execute(DDL)
    existing = dict(conn.execute(f"SELECT number, content_hash FROM {LB_TABLE}").fetchall())
print(f"[lakebase_sync] {len(existing)} row(s) already in Lakebase")

# COMMAND ----------

# Step 2: assemble the serving rows from Lakeflow silver + location + enrichment,
# keep only new/changed tickets, and embed just those.
from pyspark.sql import functions as F

rows = spark.sql(f"""
SELECT
  sn.number, sn.title, sn.status, sn.priority_label, sn.location,
  s.location_state, s.location_site, sn.assigned_to, sn.opened_date, sn.closed_date,
  e.problem_category, e.root_cause, e.resolution, e.resolution_type,
  concat_ws('\\n\\n',
    concat('Ticket ', sn.number, ': ', coalesce(sn.title, '')),
    concat('Location: ', coalesce(sn.location, '')),
    sn.description, sn.notes, sn.close_notes,
    CASE WHEN e.root_cause IS NOT NULL THEN concat('Root cause: ', e.root_cause) END,
    CASE WHEN e.resolution IS NOT NULL THEN concat('Resolution: ', e.resolution) END
  ) AS lakebase_text,
  sha2(concat_ws('||', sn.content_hash, e.root_cause, e.resolution, e.resolution_type), 256) AS content_hash
FROM {T_SN_SILVER} sn
LEFT JOIN {T_SILVER} s ON s.number = sn.number
LEFT JOIN {T_GOLD}   e ON e.number = sn.number
""")

changed_keys = [r.number for r in rows.select("number", "content_hash").collect()
                if existing.get(r.number) != r.content_hash]
print(f"[lakebase_sync] {rows.count()} silver ticket(s), {len(changed_keys)} new or changed")

# COMMAND ----------

if not changed_keys:
    dbutils.notebook.exit("no changes: 0 embeddings, 0 writes")

todo = (rows.where(F.col("number").isin(changed_keys))
            .withColumn("lakebase_vector", F.expr(f"ai_query('{EMBED}', lakebase_text)")))
batch = todo.collect()

# COMMAND ----------

# Step 3: upsert into Lakebase, one transaction.
COLS = ["number", "title", "status", "priority_label", "location", "location_state",
        "location_site", "assigned_to", "opened_date", "closed_date", "problem_category",
        "root_cause", "resolution", "resolution_type", "lakebase_text", "content_hash"]

placeholders = ", ".join(["%s"] * len(COLS)) + ", %s::vector, now()"
updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLS[1:] + ["lakebase_vector", "synced_at"])
UPSERT = f"""
INSERT INTO {LB_TABLE} ({", ".join(COLS)}, lakebase_vector, synced_at)
VALUES ({placeholders})
ON CONFLICT (number) DO UPDATE SET {updates}
"""


def vec_literal(v):
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


params = [tuple(r[c] for c in COLS) + (vec_literal(r["lakebase_vector"]),) for r in batch]
with connect() as conn:
    with conn.cursor() as cur:
        cur.executemany(UPSERT, params)
    total = conn.execute(f"SELECT count(*) FROM {LB_TABLE}").fetchone()[0]
    # IVFFlat clusters on existing rows, so build it only once there is data.
    # lists ~ rows/1000, floor 10, is plenty at this corpus size.
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{LB_TABLE}_vector ON {LB_TABLE} "
        f"USING ivfflat (lakebase_vector vector_cosine_ops) WITH (lists = {max(10, total // 1000)})"
    )

print(f"[lakebase_sync] upserted {len(params)} row(s); {LB_TABLE} now has {total}")
