"""FIS v2 Knowledge Agent — Gradio Chat App.

A conversational agent for R&D field operations with three tools:
1. Hybrid vector + full-text search over Lakebase R&D task knowledge base
2. Glossary lookup for FIS domain-specific terms and acronyms
3. Genie query for quantitative/analytical questions (SQL-backed)
"""

import os
import json
import time
import logging
import psycopg
import gradio as gr
from difflib import get_close_matches
from databricks.sdk import WorkspaceClient
from mlflow.deployments import get_deploy_client
from langchain_core.tools import tool
from databricks_langchain import ChatDatabricks
from langgraph.prebuilt import create_react_agent
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
LAKEBASE_HOST = os.getenv(
    "PGHOST",
    "ep-long-feather-d20bt18w.database.us-east-1.cloud.databricks.com",
)
LAKEBASE_DB = os.getenv("PGDATABASE", "databricks_postgres")
LAKEBASE_TABLE = "fis_tasks"
EMBEDDING_MODEL = "databricks-gte-large-en"
LLM_ENDPOINT = "databricks-claude-sonnet-4-5"
TOP_K = 5
ENDPOINT_FULL = "projects/fis/branches/production/endpoints/primary"
GLOSSARY_TABLE = "serverless_stable_l26d62_catalog.fis_knowledge_agent.glossary"
GENIE_SPACE_ID = "01f190db953e1140b39d10f49a46aa7b"

# ---------------------------------------------------------------------------
# Clients (initialized once at startup)
# ---------------------------------------------------------------------------
w = WorkspaceClient()  # auto-authenticates via SP credentials
deploy_client = get_deploy_client("databricks")

logger.info("Clients initialized")


# ---------------------------------------------------------------------------
# Glossary pre-load (94 terms — loaded once at startup)
# ---------------------------------------------------------------------------
def _load_glossary() -> tuple[list[dict], dict]:
    """Load glossary from UC table via SQL Statement Execution API."""
    try:
        # Find a serverless SQL warehouse
        warehouses = list(w.warehouses.list())
        wh_id = None
        for wh in warehouses:
            if wh.state and wh.state.value == "RUNNING":
                wh_id = wh.id
                break
        if not wh_id and warehouses:
            wh_id = warehouses[0].id
        if not wh_id:
            logger.warning("No SQL warehouse found — glossary unavailable")
            return [], {}

        resp = w.statement_execution.execute_statement(
            warehouse_id=wh_id,
            statement=(
                f"SELECT term, definition, aliases, category, source_refs "
                f"FROM {GLOSSARY_TABLE} WHERE status = 'approved'"
            ),
            wait_timeout="30s",
        )
        if not resp.result or not resp.result.data_array:
            logger.warning("Glossary query returned no rows")
            return [], {}

        cols = [c.name for c in resp.manifest.schema.columns]
        glossary = []
        for row in resp.result.data_array:
            entry = dict(zip(cols, row))
            # aliases comes back as a JSON string from the array column
            if isinstance(entry.get("aliases"), str):
                try:
                    entry["aliases"] = json.loads(entry["aliases"])
                except (json.JSONDecodeError, TypeError):
                    entry["aliases"] = []
            if isinstance(entry.get("source_refs"), str):
                try:
                    entry["source_refs"] = json.loads(entry["source_refs"])
                except (json.JSONDecodeError, TypeError):
                    entry["source_refs"] = []
            glossary.append(entry)

        # Build index: term/alias -> entry
        index = {}
        for g in glossary:
            index[g["term"].lower()] = g
            if g.get("aliases"):
                for alias in g["aliases"]:
                    if alias:
                        index[alias.lower()] = g
        logger.info("Loaded %d glossary terms (%d index entries)", len(glossary), len(index))
        return glossary, index
    except Exception as e:
        logger.warning("Failed to load glossary: %s", e)
        return [], {}


GLOSSARY, _GLOSSARY_INDEX = _load_glossary()


# ---------------------------------------------------------------------------
# Lakebase connection
# ---------------------------------------------------------------------------
def get_lakebase_connection():
    """Return a fresh psycopg3 connection using Lakebase OAuth credentials."""
    # Prefer auto-injected native PG creds when available (Lakebase resource)
    pg_user = os.getenv("PGUSER")
    pg_pass = os.getenv("PGPASSWORD")
    if pg_user and pg_pass:
        return psycopg.connect(
            host=LAKEBASE_HOST,
            dbname=LAKEBASE_DB,
            user=pg_user,
            password=pg_pass,
            sslmode="require",
        )
    # Fallback: SDK OAuth token (1-hour expiry, fine per-request)
    token = w.postgres.generate_database_credential(
        endpoint=ENDPOINT_FULL
    ).token
    username = w.current_user.me().user_name
    return psycopg.connect(
        host=LAKEBASE_HOST,
        dbname=LAKEBASE_DB,
        user=username,
        password=token,
        sslmode="require",
    )


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------
def embed_query(text: str) -> list[float]:
    """Embed a single text string using databricks-gte-large-en (1024-dim)."""
    resp = deploy_client.predict(
        endpoint=EMBEDDING_MODEL, inputs={"input": [text]}
    )
    return resp.data[0]["embedding"]


# ---------------------------------------------------------------------------
# Hybrid-search retriever tool
# ---------------------------------------------------------------------------
@tool
def fis_knowledge_search(
    query: str,
    location_state: Optional[str] = None,
    status_filter: Optional[str] = None,
) -> str:
    """Search the FIS R&D task knowledge base for information about equipment
    issues, troubleshooting procedures, root causes, and resolutions.

    Use this tool when the user asks about:
    - Equipment problems (cameras, modems, sensors, enclosures)
    - Troubleshooting steps for field equipment
    - Root cause analysis of past incidents
    - Resolution and repair procedures
    - Site-specific or location-specific issues

    Args:
        query: Natural language search query describing what to find.
        location_state: Optional 2-letter state code to filter (e.g. 'TX').
        status_filter: Optional status filter (e.g. 'Closed', 'Open').
    """
    query_embedding = embed_query(query)
    emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"

    where_clauses: list[str] = []
    params: list[str] = []
    if location_state:
        where_clauses.append("location_state = %s")
        params.append(location_state)
    if status_filter:
        where_clauses.append("status = %s")
        params.append(status_filter)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    # Combine filter conditions with full-text match for text_search
    text_conditions = list(where_clauses)
    text_conditions.append(
        "to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', %s)"
    )
    text_where = "WHERE " + " AND ".join(text_conditions)

    sql = f"""
    WITH vector_search AS (
        SELECT number, title, location_state, location_site, status,
               priority_label, problem_category, root_cause, resolution,
               resolution_type, lakebase_text,
               1 - (lakebase_vector <=> %s::vector) AS vector_score
        FROM {LAKEBASE_TABLE}
        {where_sql}
        ORDER BY lakebase_vector <=> %s::vector
        LIMIT {TOP_K * 2}
    ),
    text_search AS (
        SELECT number,
               ts_rank_cd(
                   to_tsvector('english', lakebase_text),
                   plainto_tsquery('english', %s)
               ) AS text_score
        FROM {LAKEBASE_TABLE}
        {text_where}
    )
    SELECT v.number, v.title, v.location_state, v.location_site,
           v.status, v.priority_label, v.problem_category,
           v.root_cause, v.resolution, v.resolution_type,
           v.lakebase_text, v.vector_score,
           COALESCE(t.text_score, 0) AS text_score,
           (0.7 * v.vector_score
            + 0.3 * COALESCE(t.text_score, 0)) AS combined_score
    FROM vector_search v
    LEFT JOIN text_search t ON v.number = t.number
    ORDER BY combined_score DESC
    LIMIT {TOP_K}
    """

    all_params = (
        [emb_str] + params + [emb_str] + [query] + params + [query]
    )

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

    results = []
    for row in rows:
        r = dict(zip(col_names, row))
        results.append(
            f"**{r['number']}** \u2014 {r['title']}\n"
            f"Location: {r['location_state'] or 'N/A'}"
            f" | {r['location_site'] or 'N/A'}\n"
            f"Status: {r['status']} | Priority: {r['priority_label']}\n"
            f"Category: {r['problem_category'] or 'N/A'}\n"
            f"Root Cause: {r['root_cause'] or 'N/A'}\n"
            f"Resolution: {r['resolution'] or 'N/A'}"
            f" ({r['resolution_type'] or 'N/A'})\n"
            f"---\nFull Content:\n{r['lakebase_text']}"
        )
    return "\n\n===\n\n".join(results)


# ---------------------------------------------------------------------------
# Glossary lookup tool
# ---------------------------------------------------------------------------
@tool
def glossary_lookup(term: str) -> str:
    """Look up the definition of an FIS domain-specific term, acronym, or
    equipment name such as OVC, PIPS, Kistler, AUR, WIM, ALPR, CA, Neology.

    Args:
        term: The term or acronym to look up.
    """
    if not GLOSSARY:
        return "Glossary is not available."

    term_lower = term.lower().strip()

    # Exact match on term or alias
    if term_lower in _GLOSSARY_INDEX:
        g = _GLOSSARY_INDEX[term_lower]
        aliases = ", ".join(g["aliases"]) if g.get("aliases") else "none"
        return (
            f"**{g['term']}** ({g['category']})\n"
            f"Definition: {g['definition']}\n"
            f"Aliases: {aliases}"
        )

    # Fuzzy match
    all_terms = [g["term"] for g in GLOSSARY]
    matches = get_close_matches(term, all_terms, n=3, cutoff=0.5)
    if matches:
        results = []
        for m in matches:
            g = next(g for g in GLOSSARY if g["term"] == m)
            results.append(f"**{g['term']}** ({g['category']}): {g['definition']}")
        return "No exact match. Similar terms:\n" + "\n".join(results)

    return f"Term '{term}' not found in the FIS glossary."


# ---------------------------------------------------------------------------
# Genie query tool (quantitative analytics)
# ---------------------------------------------------------------------------
@tool
def genie_query(question: str) -> str:
    """Query the FIS analytics Genie space for quantitative, data-driven answers.

    Use for questions needing counts, rankings, aggregations, or statistics:
    - "How many sites have the most brake issues?"
    - "Which state has the most open tasks?"
    - "Task counts by priority"

    Do NOT use for qualitative troubleshooting or root cause questions.

    Args:
        question: The analytical or quantitative question to ask.
    """
    try:
        op = w.genie.start_conversation(
            space_id=GENIE_SPACE_ID, content=question
        )
        # Poll until complete (max 90s)
        msg = None
        try:
            msg = op.result(timeout=time.monotonic() + 90)
        except Exception:
            # Manual polling fallback
            resp = op.response
            conv_id = resp.conversation_id
            msg_id = resp.message_id
            for _ in range(45):
                msg = w.genie.get_message(
                    space_id=GENIE_SPACE_ID,
                    conversation_id=conv_id,
                    message_id=msg_id,
                )
                if msg.status and msg.status.value in (
                    "COMPLETED", "FAILED", "CANCELLED",
                ):
                    break
                time.sleep(2)
            else:
                return "Genie query timed out. Try a simpler question."

        if msg is None:
            return "Genie query returned no response."

        if msg.status and msg.status.value == "FAILED":
            err = msg.error
            return f"Genie could not answer: {err.error if err else 'unknown'}"

        # Extract text + SQL + results from attachments
        parts = []
        if msg.attachments:
            for att in msg.attachments:
                if att.text and att.text.content:
                    parts.append(att.text.content)
                if att.query:
                    if att.query.description:
                        parts.append(att.query.description)
                    if att.query.query:
                        parts.append(
                            f"\nSQL used:\n```sql\n{att.query.query}\n```"
                        )
                    if att.attachment_id:
                        try:
                            qr = w.genie.get_message_query_result_by_attachment(
                                space_id=GENIE_SPACE_ID,
                                conversation_id=msg.conversation_id,
                                message_id=msg.message_id,
                                attachment_id=att.attachment_id,
                            )
                            sr = qr.statement_response
                            if sr and sr.result and sr.result.data_array:
                                cols = [c.name for c in sr.manifest.schema.columns]
                                rows = sr.result.data_array
                                header = " | ".join(cols)
                                lines = [header, " | ".join("---" for _ in cols)]
                                for row in rows[:20]:
                                    lines.append(" | ".join(str(v) for v in row))
                                if len(rows) > 20:
                                    lines.append(f"... ({len(rows)} total rows)")
                                parts.append("\nResults:\n" + "\n".join(lines))
                        except Exception:
                            pass

        return "\n".join(parts) if parts else "Genie returned no answer."
    except Exception as e:
        logger.exception("Genie query error")
        return f"Genie query error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# LangGraph agent (3 tools)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are the FIS v2 Knowledge Agent — an expert assistant \
for the R&D field operations team.

You have THREE tools. Choose the right one based on the question type:

1. **fis_knowledge_search** — for qualitative questions about equipment issues, \
troubleshooting, root causes, and resolutions.

2. **glossary_lookup** — for unknown terms, acronyms, or FIS-specific jargon \
(OVC, PIPS, WIM, AUR, Kistler, Neology, CA, etc.). Use FIRST when you encounter \
an unfamiliar term, then chain with fis_knowledge_search.

3. **genie_query** — for quantitative/analytical questions needing data: \
counts, rankings, aggregations, comparisons, trends.

Routing:
- "how many", "count", "top N", "rank", "most", "percentage" → genie_query
- troubleshooting, root cause, resolution, specific task → fis_knowledge_search
- unfamiliar acronym or term → glossary_lookup first, then search
- You CAN use multiple tools in one turn.

When answering:
1. Cite task numbers (e.g., R&DTASK0002200) when referencing past incidents.
2. Pass location_state filter for location-specific questions.
3. Synthesize from multiple tasks when applicable.
4. Do not fabricate — say clearly if nothing is found.
5. For Genie results, present the data table.
"""

llm = ChatDatabricks(endpoint=LLM_ENDPOINT)
agent = create_react_agent(
    model=llm,
    tools=[fis_knowledge_search, glossary_lookup, genie_query],
    prompt=SYSTEM_PROMPT,
)

logger.info("Agent ready (3 tools)")


# ---------------------------------------------------------------------------
# Gradio chat interface
# ---------------------------------------------------------------------------
def chat_fn(message: str, history: list) -> str:
    """Handle a single chat turn."""
    try:
        result = agent.invoke(
            {"messages": [{"role": "user", "content": message}]}
        )
        for msg in reversed(result["messages"]):
            if hasattr(msg, "type") and msg.type == "ai" and msg.content:
                return msg.content
        return "I couldn't find relevant information for your query."
    except Exception as e:
        logger.exception("Agent error")
        return f"An error occurred: {e}"


demo = gr.ChatInterface(
    fn=chat_fn,
    type="messages",
    title="\U0001f50d FIS v2 Knowledge Agent",
    description=(
        "Ask about R&D equipment issues, troubleshooting procedures, "
        "root causes, and resolutions from past field tasks."
    ),
    examples=[
        "What are common camera alignment issues and how were they resolved?",
        "Show me modem connectivity problems in Virginia",
        "What does OVC stand for?",
        "How many R&D tasks are open by state?",
        "What is Kistler?",
        "Which assignment groups have the most tasks?",
    ],
    theme=gr.themes.Soft(),
)

if __name__ == "__main__":
    app_port = int(os.getenv("DATABRICKS_APP_PORT", "8000"))
    demo.launch(server_name="0.0.0.0", server_port=app_port)
