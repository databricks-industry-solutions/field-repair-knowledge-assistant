"""FIS v2 Knowledge Agent — Gradio Chat App.

A conversational agent for R&D field operations with three tools:
1. Hybrid vector + full-text search over Lakebase R&D task knowledge base
2. Glossary lookup for FIS domain-specific terms and acronyms
3. Genie query for quantitative/analytical questions (SQL-backed)
"""

import os
import re
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
# Everything workspace-specific comes from app.yaml env, so the same code deploys anywhere.
RKB_CATALOG = os.getenv("RKB_CATALOG", "main")
RKB_SCHEMA = os.getenv("RKB_SCHEMA", "troubleshooting_knowledge_agent")
ENDPOINT_FULL = os.getenv("LAKEBASE_ENDPOINT", "projects/fis/branches/production/endpoints/primary")
LAKEBASE_HOST = os.getenv("PGHOST")  # resolved from ENDPOINT_FULL at startup when unset
LAKEBASE_DB = os.getenv("PGDATABASE", "databricks_postgres")
LAKEBASE_TABLE = os.getenv("LAKEBASE_TABLE", "fis_tasks")
EMBEDDING_MODEL = "databricks-gte-large-en"
LLM_ENDPOINT = "databricks-claude-sonnet-4-5"
TOP_K = 5
GLOSSARY_TABLE = os.getenv("GLOSSARY_TABLE") or f"{RKB_CATALOG}.{RKB_SCHEMA}.glossary"
GENIE_SPACE_ID = os.getenv("GENIE_SPACE_ID", "")

# ---------------------------------------------------------------------------
# Clients (initialized once at startup)
# ---------------------------------------------------------------------------
w = WorkspaceClient()  # auto-authenticates via SP credentials
deploy_client = get_deploy_client("databricks")

if not LAKEBASE_HOST:
    LAKEBASE_HOST = w.postgres.get_endpoint(name=ENDPOINT_FULL).status.hosts.host

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


def expand_with_glossary(query: str) -> list[str]:
    """Governed terms and aliases named in the query, expanded to every known name.

    "power controller" -> ["WPS", "PowerNode", "power controller", "Web Power Switch"],
    so search finds a ticket whichever name the tech wrote. Whole-word matches only,
    so a short term like CA doesn't fire on "camera".
    """
    q = query.lower()
    names: list[str] = []
    for key, g in _GLOSSARY_INDEX.items():
        if re.search(rf"\b{re.escape(key)}\b", q):
            for n in [g["term"], *(g.get("aliases") or [])]:
                if n and n not in names:
                    names.append(n)
    return names


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
    synonyms = expand_with_glossary(query)
    embed_text = f"{query} ({'; '.join(synonyms)})" if synonyms else query
    query_embedding = embed_query(embed_text)
    emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    # Full-text: any governed name matches; otherwise the plain query.
    ts_fn = "websearch_to_tsquery" if synonyms else "plainto_tsquery"
    ts_query = " or ".join(f'"{n}"' for n in synonyms) if synonyms else query
    if synonyms:
        logger.info("glossary expansion: %s", synonyms)

    where_clauses: list[str] = []
    params: list[str] = []
    if location_state:
        where_clauses.append("location_state = %s")
        params.append(location_state)
    if status_filter:
        where_clauses.append("status = %s")
        params.append(status_filter)

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    tsv = "to_tsvector('english', lakebase_text)"
    tsq = f"{ts_fn}('english', %s)"
    text_where = "WHERE " + " AND ".join(where_clauses + [f"{tsv} @@ {tsq}"])
    # A governed-term match is strong evidence on its own, so it scores 1. Otherwise
    # fall back to ts_rank_cd. Candidates are the UNION of the vector and full-text
    # top hits, so a ticket that only matches on wording still gets ranked.
    text_score = (f"CASE WHEN {tsv} @@ {tsq} THEN 1.0 ELSE 0 END" if synonyms
                  else f"COALESCE(ts_rank_cd({tsv}, {tsq}), 0)")

    sql = f"""
    WITH vec AS (
        SELECT number FROM {LAKEBASE_TABLE} {where_sql}
        ORDER BY lakebase_vector <=> %s::vector LIMIT {TOP_K * 2}
    ),
    txt AS (
        SELECT number FROM {LAKEBASE_TABLE} {text_where}
        ORDER BY ts_rank_cd({tsv}, {tsq}) DESC LIMIT {TOP_K * 2}
    ),
    cand AS (SELECT number FROM vec UNION SELECT number FROM txt)
    SELECT t.number, t.title, t.location_state, t.location_site,
           t.status, t.priority_label, t.problem_category,
           t.root_cause, t.resolution, t.resolution_type, t.lakebase_text,
           1 - (t.lakebase_vector <=> %s::vector) AS vector_score,
           {text_score} AS text_score,
           0.7 * (1 - (t.lakebase_vector <=> %s::vector)) + 0.3 * ({text_score}) AS combined_score
    FROM {LAKEBASE_TABLE} t JOIN cand USING (number)
    ORDER BY combined_score DESC
    LIMIT {TOP_K}
    """

    # Placeholders in SQL order; each text_score expression takes one ts_query.
    all_params = (
        params + [emb_str]                       # vec: filters, ORDER BY distance
        + params + [ts_query, ts_query]          # txt: filters + match, ORDER BY rank
        + [emb_str, ts_query]                    # vector_score, text_score
        + [emb_str, ts_query]                    # combined_score
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
    if synonyms:
        # Tell the model the governed mapping, because the ticket text usually uses
        # only one of the names (a WPS ticket never says "power controller").
        results.append(
            "Glossary (governed): these names refer to the same thing in the tickets: "
            + ", ".join(synonyms)
            + ". Treat a ticket that mentions any of them as relevant."
        )
    for row in rows:
        r = dict(zip(col_names, row))
        if synonyms:
            text = (r["lakebase_text"] or "").lower()
            hit = [n for n in synonyms if n.lower() in text]
            if hit:
                r["title"] = f"{r['title']}  [mentions {', '.join(hit)}]"
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
    logger.info("genie_query: %s", question)
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
    title="Field Repair Knowledge Agent",
    description=(
        "Ask about R&D equipment issues, troubleshooting procedures, "
        "root causes, and resolutions from past field tasks."
    ),
    examples=[
        "A WIM site is reporting 0 weights. Have we seen this before and what fixed it?",
        "What fixed power controller issues at our sites?",
        "Which open tasks should we triage first?",
        "Who is our expert for WIM issues?",
        "What does OVC stand for?",
    ],
    theme=gr.themes.Soft(),
)

if __name__ == "__main__":
    app_port = int(os.getenv("DATABRICKS_APP_PORT", "8000"))
    demo.launch(server_name="0.0.0.0", server_port=app_port)
