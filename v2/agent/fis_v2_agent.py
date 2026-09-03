# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Install dependencies
# MAGIC %pip install psycopg[binary]>=3.1.0 databricks-langchain langgraph --upgrade mlflow>=2.14 databricks-agents
# MAGIC import subprocess, sys
# MAGIC # Side-load newer databricks-sdk (for w.postgres) and langgraph (version compat fix)
# MAGIC for pkg in ["databricks-sdk>=0.118.0", "langgraph>=1.2.0", "langgraph-prebuilt>=1.1.0", "langgraph-sdk>=0.4.0"]:
# MAGIC     subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", "/tmp/new_sdk", "--no-deps", "--ignore-installed", "--no-warn-conflicts", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration
# === Configuration ===
LAKEBASE_PROJECT = "fis"
LAKEBASE_BRANCH = "production"
LAKEBASE_ENDPOINT = "primary"
LAKEBASE_DB = "databricks_postgres"
LAKEBASE_HOST = "ep-long-feather-d20bt18w.database.us-east-1.cloud.databricks.com"
LAKEBASE_TABLE = "fis_tasks"
EMBEDDING_MODEL = "databricks-gte-large-en"
LLM_ENDPOINT = "databricks-claude-sonnet-4-5"
TOP_K = 5

GLOSSARY_TABLE = "serverless_stable_l26d62_catalog.fis_knowledge_agent.glossary"
GENIE_SPACE_ID = "01f190db953e1140b39d10f49a46aa7b"

# Derived
ENDPOINT_FULL = f"projects/{LAKEBASE_PROJECT}/branches/{LAKEBASE_BRANCH}/endpoints/{LAKEBASE_ENDPOINT}"

# COMMAND ----------

# DBTITLE 1,Lakebase connection helper
import sys
sys.path.insert(0, "/tmp/new_sdk")
import importlib
import databricks.sdk
importlib.reload(databricks.sdk)
# Also reload langgraph from side-loaded packages
import langgraph
importlib.reload(langgraph)

import psycopg
from databricks.sdk import WorkspaceClient

w = WorkspaceClient()

def get_lakebase_connection():
    """Create a fresh psycopg connection to the Lakebase fis project."""
    token = w.postgres.generate_database_credential(endpoint=ENDPOINT_FULL).token
    username = w.current_user.me().user_name
    return psycopg.connect(
        host=LAKEBASE_HOST, dbname=LAKEBASE_DB,
        user=username, password=token, sslmode="require"
    )

# Test connection
with get_lakebase_connection() as conn:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {LAKEBASE_TABLE}")
    print(f"Connected to Lakebase fis — {cur.fetchone()[0]} rows in {LAKEBASE_TABLE}")

# COMMAND ----------

# DBTITLE 1,Embedding helper
from mlflow.deployments import get_deploy_client

deploy_client = get_deploy_client("databricks")

def embed_query(text: str) -> list[float]:
    """Embed a single query string using databricks-gte-large-en."""
    resp = deploy_client.predict(
        endpoint=EMBEDDING_MODEL,
        inputs={"input": [text]}
    )
    return resp.data[0]["embedding"]

# Test
test_emb = embed_query("camera misalignment")
print(f"Embedding dim: {len(test_emb)}")

# COMMAND ----------

# DBTITLE 1,Hybrid search retriever tool
from langchain_core.tools import tool
from typing import Optional

@tool
def fis_knowledge_search(query: str, location_state: Optional[str] = None, status_filter: Optional[str] = None) -> str:
    """Search the FIS R&D task knowledge base for information about equipment issues,
    troubleshooting procedures, root causes, and resolutions.

    Use this tool when the user asks about:
    - Equipment problems (cameras, modems, sensors, enclosures)
    - Troubleshooting steps for field equipment
    - Root cause analysis of past incidents
    - Resolution and repair procedures
    - Site-specific or location-specific issues

    Args:
        query: Natural language search query describing what to find.
        location_state: Optional 2-letter state code to filter results (e.g., 'TX', 'VA').
        status_filter: Optional status filter (e.g., 'Closed', 'Open').
    """
    # 1. Embed the query
    query_embedding = embed_query(query)
    emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    # 2. Build hybrid query: combine vector similarity + full-text ranking
    where_clauses = []
    params = []
    if location_state:
        where_clauses.append("location_state = %s")
        params.append(location_state)
    if status_filter:
        where_clauses.append("status = %s")
        params.append(status_filter)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    # Combine filter conditions with full-text match for text_search
    text_conditions = list(where_clauses)
    text_conditions.append("to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', %s)")
    text_where = "WHERE " + " AND ".join(text_conditions)

    sql = f"""
    WITH vector_search AS (
        SELECT number, title, location_state, location_site, status, priority_label,
               problem_category, root_cause, resolution, resolution_type,
               lakebase_text,
               1 - (lakebase_vector <=> %s::vector) AS vector_score
        FROM {LAKEBASE_TABLE}
        {where_sql}
        ORDER BY lakebase_vector <=> %s::vector
        LIMIT {TOP_K * 2}
    ),
    text_search AS (
        SELECT number,
               ts_rank_cd(to_tsvector('english', lakebase_text), plainto_tsquery('english', %s)) AS text_score
        FROM {LAKEBASE_TABLE}
        {text_where}
    )
    SELECT v.number, v.title, v.location_state, v.location_site, v.status, v.priority_label,
           v.problem_category, v.root_cause, v.resolution, v.resolution_type,
           v.lakebase_text,
           v.vector_score,
           COALESCE(t.text_score, 0) AS text_score,
           (0.7 * v.vector_score + 0.3 * COALESCE(t.text_score, 0)) AS combined_score
    FROM vector_search v
    LEFT JOIN text_search t ON v.number = t.number
    ORDER BY combined_score DESC
    LIMIT {TOP_K}
    """

    # Build params list
    all_params = [emb_str] + params + [emb_str] + [query] + params + [query]

    # 3. Execute
    conn = get_lakebase_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, all_params)
        rows = cur.fetchall()
        col_names = [desc[0] for desc in cur.description]
    finally:
        conn.close()

    if not rows:
        return "No matching R&D tasks found for your query."

    # 4. Format results
    results = []
    for row in rows:
        r = dict(zip(col_names, row))
        results.append(
            f"**{r['number']}** — {r['title']}\n"
            f"Location: {r['location_state'] or 'N/A'} | {r['location_site'] or 'N/A'}\n"
            f"Status: {r['status']} | Priority: {r['priority_label']}\n"
            f"Category: {r['problem_category'] or 'N/A'}\n"
            f"Root Cause: {r['root_cause'] or 'N/A'}\n"
            f"Resolution: {r['resolution'] or 'N/A'} ({r['resolution_type'] or 'N/A'})\n"
            f"Relevance: vector={r['vector_score']:.3f}, text={r['text_score']:.3f}\n"
            f"---\nFull Content:\n{r['lakebase_text']}"
        )
    return "\n\n===\n\n".join(results)

# Test the tool
print(fis_knowledge_search.invoke({"query": "camera misalignment"})[:500])

# COMMAND ----------

# DBTITLE 1,Glossary lookup tool
from difflib import get_close_matches

# Pre-load the 94-term glossary into memory (small dataset — no runtime query needed)
glossary_df = spark.sql(f"SELECT term, definition, aliases, category, source_refs FROM {GLOSSARY_TABLE} WHERE status = 'approved'")
GLOSSARY = [row.asDict() for row in glossary_df.collect()]
# Build a flat lookup: term -> row, alias -> row
_GLOSSARY_INDEX = {}
for g in GLOSSARY:
    _GLOSSARY_INDEX[g["term"].lower()] = g
    if g["aliases"]:
        for alias in g["aliases"]:
            _GLOSSARY_INDEX[alias.lower()] = g

print(f"Loaded {len(GLOSSARY)} glossary terms ({len(_GLOSSARY_INDEX)} index entries incl. aliases)")

@tool
def glossary_lookup(term: str) -> str:
    """Look up the definition of an FIS domain-specific term, acronym, or equipment name.

    Use this tool when you encounter an unfamiliar term or acronym such as
    OVC, PIPS, Kistler, AUR, WIM, ALPR, CA, Neology, Fleetworthy, etc.

    Args:
        term: The term or acronym to look up.
    """
    term_lower = term.lower().strip()

    # Exact match on term or alias
    if term_lower in _GLOSSARY_INDEX:
        g = _GLOSSARY_INDEX[term_lower]
        aliases = ", ".join(g["aliases"]) if g["aliases"] else "none"
        return (
            f"**{g['term']}** ({g['category']})\n"
            f"Definition: {g['definition']}\n"
            f"Aliases: {aliases}"
        )

    # Fuzzy match against all terms
    all_terms = [g["term"] for g in GLOSSARY]
    matches = get_close_matches(term, all_terms, n=3, cutoff=0.5)
    if matches:
        results = []
        for m in matches:
            g = next(g for g in GLOSSARY if g["term"] == m)
            results.append(f"**{g['term']}** ({g['category']}): {g['definition']}")
        return "No exact match found. Similar terms:\n" + "\n".join(results)

    return f"Term '{term}' not found in the FIS glossary (94 approved terms)."

# Test
print("---")
print(glossary_lookup.invoke({"term": "OVC"}))
print("---")
print(glossary_lookup.invoke({"term": "Neology"}))

# COMMAND ----------

# DBTITLE 1,Genie query tool (quantitative analytics)
import time as _time

@tool
def genie_query(question: str) -> str:
    """Query the FIS analytics Genie space for quantitative, data-driven answers.

    Use this tool when the user asks questions that require counting, ranking,
    aggregation, or statistical analysis, such as:
    - "How many sites have the most brake issues?"
    - "Which state has the most open tasks?"
    - "Show task counts by priority"
    - "What percentage of tasks are closed?"
    - "Top 5 assignment groups by volume"

    Do NOT use this tool for qualitative questions about troubleshooting,
    root cause analysis, or resolution procedures — use fis_knowledge_search instead.

    Args:
        question: The analytical or quantitative question to ask.
    """
    try:
        # start_conversation returns a Wait[GenieMessage] (LRO)
        op = w.genie.start_conversation(space_id=GENIE_SPACE_ID, content=question)
        # Poll until complete (timeout 90s)
        msg = op.result(timeout=_time.monotonic() + 90)
    except TimeoutError:
        return "Genie query timed out after 90 seconds. Try a simpler question."
    except Exception as e:
        # Fallback: try manual polling
        try:
            resp = op.response  # the initial response with conversation_id/message_id
            conv_id = resp.conversation_id
            msg_id = resp.message_id
            for _ in range(45):
                msg = w.genie.get_message(
                    space_id=GENIE_SPACE_ID,
                    conversation_id=conv_id,
                    message_id=msg_id,
                )
                if msg.status and msg.status.value in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                _time.sleep(2)
            else:
                return "Genie query timed out. Try a simpler question."
        except Exception as poll_err:
            return f"Genie query error: {type(e).__name__}: {e}"

    # Check for errors
    if msg.status and msg.status.value == "FAILED":
        err = msg.error
        return f"Genie could not answer: {err.error if err else 'unknown error'}"

    # Extract text + SQL from attachments
    parts = []
    if msg.attachments:
        for att in msg.attachments:
            if att.text and att.text.content:
                parts.append(att.text.content)
            if att.query:
                if att.query.description:
                    parts.append(att.query.description)
                if att.query.query:
                    parts.append(f"\nSQL used:\n```sql\n{att.query.query}\n```")
                # Fetch query results if available
                if att.attachment_id:
                    try:
                        qr = w.genie.get_message_query_result_by_attachment(
                            space_id=GENIE_SPACE_ID,
                            conversation_id=msg.conversation_id,
                            message_id=msg.message_id,
                            attachment_id=att.attachment_id,
                        )
                        if qr.statement_response and qr.statement_response.result:
                            cols = [c.name for c in qr.statement_response.manifest.schema.columns]
                            rows = qr.statement_response.result.data_array or []
                            # Format as a table (max 20 rows)
                            header = " | ".join(cols)
                            lines = [header, " | ".join("---" for _ in cols)]
                            for row in rows[:20]:
                                lines.append(" | ".join(str(v) for v in row))
                            if len(rows) > 20:
                                lines.append(f"... ({len(rows)} total rows)")
                            parts.append("\nResults:\n" + "\n".join(lines))
                    except Exception:
                        pass  # results not critical

    if parts:
        return "\n".join(parts)
    return "Genie returned no answer for this question."

# Test
print(genie_query.invoke({"question": "How many R&D tasks are there by status?"}))

# COMMAND ----------

# DBTITLE 1,Define the LangGraph agent (3 tools)
from databricks_langchain import ChatDatabricks
from langgraph.prebuilt import create_react_agent

llm = ChatDatabricks(endpoint=LLM_ENDPOINT)

SYSTEM_PROMPT = """You are the FIS v2 Knowledge Agent — an expert assistant for the R&D field operations team.

You have THREE tools. Choose the right one based on the question type:

1. **fis_knowledge_search** — for qualitative questions about equipment issues,
   troubleshooting, root causes, and resolutions. Use this when the user asks
   "how do I fix...", "what caused...", "what's the resolution for...".

2. **glossary_lookup** — for unknown terms, acronyms, or FIS-specific jargon.
   Use this FIRST when you encounter an unfamiliar term (OVC, PIPS, WIM, AUR,
   Kistler, Neology, CA, etc.) so you can understand the question before answering.
   You may chain this with fis_knowledge_search: look up the term, then search
   for related issues.

3. **genie_query** — for quantitative/analytical questions that need data:
   counts, rankings, aggregations, comparisons, percentages, trends.
   Examples: "how many sites...", "which state has the most...",
   "top 5 assignment groups", "task counts by priority".

Routing rules:
- If the question contains "how many", "count", "top N", "rank", "most",
  "least", "percentage", "compare", "trend", or asks for a number → genie_query
- If the question asks about troubleshooting, root cause, resolution, or
  mentions a specific task number → fis_knowledge_search
- If you see an unfamiliar acronym or term → glossary_lookup first
- You CAN use multiple tools in one turn (e.g., glossary then search)

When answering:
1. Cite specific task numbers (e.g., R&DTASK0002200) when referencing past incidents.
2. If the user asks about a specific location or state, pass the location_state filter.
3. Synthesize information from multiple relevant tasks when applicable.
4. If no relevant tasks are found, say so clearly — do not fabricate information.
5. When describing troubleshooting steps, be specific and actionable.
6. For Genie results, present the data table and any SQL used.
"""

agent = create_react_agent(
    model=llm,
    tools=[fis_knowledge_search, glossary_lookup, genie_query],
    prompt=SYSTEM_PROMPT
)

print("Agent created successfully")

# COMMAND ----------

# DBTITLE 1,Test the agent
# Test with a sample query
result = agent.invoke({"messages": [{"role": "user", "content": "What are common camera alignment issues and how were they resolved?"}]})

# Print the final answer
for msg in result["messages"]:
    if hasattr(msg, 'type') and msg.type == 'ai' and msg.content:
        print(msg.content)

# COMMAND ----------

# DBTITLE 1,Log and register model with MLflow
import mlflow
import os

mlflow.set_registry_uri("databricks-uc")

# Write model-from-code script
model_code_lines = [
    'import sys',
    'sys.path.insert(0, "/tmp/new_sdk")',
    'import importlib, databricks.sdk',
    'importlib.reload(databricks.sdk)',
    '',
    'import psycopg',
    'from databricks.sdk import WorkspaceClient',
    'from mlflow.deployments import get_deploy_client',
    'from langchain_core.tools import tool',
    'from databricks_langchain import ChatDatabricks',
    'from langgraph.prebuilt import create_react_agent',
    'from typing import Optional',
    'import mlflow',
    '',
    'LAKEBASE_HOST = "ep-long-feather-d20bt18w.database.us-east-1.cloud.databricks.com"',
    'LAKEBASE_DB = "databricks_postgres"',
    'LAKEBASE_TABLE = "fis_tasks"',
    'EMBEDDING_MODEL = "databricks-gte-large-en"',
    'LLM_ENDPOINT = "databricks-claude-sonnet-4-5"',
    'TOP_K = 5',
    'ENDPOINT_FULL = "projects/fis/branches/production/endpoints/primary"',
    '',
    'w = WorkspaceClient()',
    'deploy_client = get_deploy_client("databricks")',
    '',
    'def embed_query(text):', 
    '    resp = deploy_client.predict(endpoint=EMBEDDING_MODEL, inputs={"input": [text]})',
    '    return resp.data[0]["embedding"]',
    '',
    'def get_lakebase_connection():',
    '    token = w.postgres.generate_database_credential(endpoint=ENDPOINT_FULL).token',
    '    username = w.current_user.me().user_name',
    '    return psycopg.connect(host=LAKEBASE_HOST, dbname=LAKEBASE_DB, user=username, password=token, sslmode="require")',
    '',
    '@tool',
    'def fis_knowledge_search(query: str, location_state: Optional[str] = None, status_filter: Optional[str] = None) -> str:',
    '    """Search FIS R&D task knowledge base for equipment issues, troubleshooting, root causes, resolutions."""',
    '    query_embedding = embed_query(query)',
    '    emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"',
    '    where_clauses, params = [], []',
    '    if location_state:',
    '        where_clauses.append("location_state = %s")',
    '        params.append(location_state)',
    '    if status_filter:',
    '        where_clauses.append("status = %s")',
    '        params.append(status_filter)',
    '    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""',
    '    sql = f"""',
    '    WITH vector_search AS (',
    '        SELECT number, title, location_state, location_site, status, priority_label,',
    '               problem_category, root_cause, resolution, resolution_type, lakebase_text,',
    '               1 - (lakebase_vector <=> %s::vector) AS vector_score',
    '        FROM {LAKEBASE_TABLE} {where_sql}',
    '        ORDER BY lakebase_vector <=> %s::vector LIMIT {TOP_K * 2}',
    '    ),',
    "    text_search AS (",
    "        SELECT number, ts_rank_cd(to_tsvector('english', lakebase_text), plainto_tsquery('english', %s)) AS text_score",
    '        FROM {LAKEBASE_TABLE} {where_sql}',
    "        WHERE to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', %s)",
    '    )',
    '    SELECT v.number, v.title, v.location_state, v.location_site, v.status, v.priority_label,',
    '           v.problem_category, v.root_cause, v.resolution, v.resolution_type, v.lakebase_text,',
    '           v.vector_score, COALESCE(t.text_score, 0) AS text_score,',
    '           (0.7 * v.vector_score + 0.3 * COALESCE(t.text_score, 0)) AS combined_score',
    '    FROM vector_search v LEFT JOIN text_search t ON v.number = t.number',
    '    ORDER BY combined_score DESC LIMIT {TOP_K}',
    '    """',
    '    all_params = [emb_str] + params + [emb_str] + params + [query] + params + [query]',
    '    conn = get_lakebase_connection()',
    '    try:',
    '        cur = conn.cursor()',
    '        cur.execute(sql, all_params)',
    '        rows = cur.fetchall()',
    '        col_names = [desc[0] for desc in cur.description]',
    '    finally:',
    '        conn.close()',
    '    if not rows:',
    '        return "No matching R&D tasks found."',
    '    results = []',
    '    for row in rows:',
    '        r = dict(zip(col_names, row))',
    '        results.append(f"**{r[\'number\']}** - {r[\'title\']}\\nRoot Cause: {r[\'root_cause\'] or \'N/A\'}\\nResolution: {r[\'resolution\'] or \'N/A\'}")',
    '    return "\\n\\n===\\n\\n".join(results)',
    '',
    'SYSTEM_PROMPT = """You are the FIS v2 Knowledge Agent. ALWAYS use fis_knowledge_search before answering. Cite task numbers."""',
    '',
    'llm = ChatDatabricks(endpoint=LLM_ENDPOINT)',
    'agent = create_react_agent(model=llm, tools=[fis_knowledge_search], prompt=SYSTEM_PROMPT)',
    'mlflow.models.set_model(agent)',
]

model_path = "/tmp/fis_v2_agent_model.py"
with open(model_path, "w") as f:
    f.write("\n".join(model_code_lines))

print("Model code written to", model_path)

input_example = {"messages": [{"role": "user", "content": "What camera issues have been reported?"}]}

with mlflow.start_run(run_name="fis_v2_agent"):
    model_info = mlflow.langchain.log_model(
        lc_model=model_path,
        artifact_path="fis_v2_agent",
        input_example=input_example,
        # Note: registration skipped due to metastore quota (5299/5000).
        # Uncomment below once quota is available:
        # registered_model_name="serverless_stable_l26d62_catalog.fis_knowledge_agent.fis_v2_agent",
    )

print(f"Model logged: {model_info.model_uri}")
print("To register later, run:")
print(f'  mlflow.register_model("{model_info.model_uri}", "serverless_stable_l26d62_catalog.fis_knowledge_agent.fis_v2_agent")')