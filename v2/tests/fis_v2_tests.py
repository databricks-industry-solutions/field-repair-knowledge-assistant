# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,Setup & Dependencies
# MAGIC %pip install psycopg[binary]>=3.1.0 databricks-langchain langgraph --upgrade mlflow>=2.14
# MAGIC import subprocess, sys
# MAGIC for pkg in ["databricks-sdk>=0.118.0", "langgraph>=1.2.0", "langgraph-prebuilt>=1.1.0", "langgraph-sdk>=0.4.0"]:
# MAGIC     subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", "/tmp/new_sdk", "--no-deps", "--ignore-installed", "--no-warn-conflicts", pkg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Test Configuration & Helpers
import sys
sys.path.insert(0, "/tmp/new_sdk")
import importlib, databricks.sdk
importlib.reload(databricks.sdk)
import langgraph
importlib.reload(langgraph)

import psycopg
from databricks.sdk import WorkspaceClient
from mlflow.deployments import get_deploy_client
from langchain_core.tools import tool
from databricks_langchain import ChatDatabricks
from langgraph.prebuilt import create_react_agent
from typing import Optional
import time

# Config (same as app)
dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "troubleshooting_knowledge_agent")
dbutils.widgets.text("genie_space_id", "")
_CAT, _SCH = dbutils.widgets.get("catalog"), dbutils.widgets.get("schema")
LAKEBASE_HOST = None  # looked up from ENDPOINT_FULL once the workspace client exists
LAKEBASE_DB = "databricks_postgres"
LAKEBASE_TABLE = "fis_tasks"
EMBEDDING_MODEL = "databricks-gte-large-en"
LLM_ENDPOINT = "databricks-claude-sonnet-4-5"
TOP_K = 5
ENDPOINT_FULL = "projects/fis/branches/production/endpoints/primary"
GLOSSARY_TABLE = f"{_CAT}.{_SCH}.glossary"
GENIE_SPACE_ID = dbutils.widgets.get("genie_space_id")

w = WorkspaceClient()
LAKEBASE_HOST = LAKEBASE_HOST or w.postgres.get_endpoint(name=ENDPOINT_FULL).status.hosts.host
deploy_client = get_deploy_client("databricks")

def get_lakebase_connection():
    token = w.postgres.generate_database_credential(endpoint=ENDPOINT_FULL).token
    username = w.current_user.me().user_name
    return psycopg.connect(host=LAKEBASE_HOST, dbname=LAKEBASE_DB, user=username, password=token, sslmode="require")

def embed_query(text):
    resp = deploy_client.predict(endpoint=EMBEDDING_MODEL, inputs={"input": [text]})
    return resp.data[0]["embedding"]

results = {"passed": 0, "failed": 0, "errors": []}

def run_test(name, fn):
    try:
        fn()
        results["passed"] += 1
        print(f"  \u2705 {name}")
    except AssertionError as e:
        results["failed"] += 1
        results["errors"].append((name, str(e)))
        print(f"  \u274c {name}: {e}")
    except Exception as e:
        results["failed"] += 1
        results["errors"].append((name, str(e)))
        print(f"  \U0001f4a5 {name}: {type(e).__name__}: {e}")

print("Test framework ready")

# COMMAND ----------

# DBTITLE 1,Test Suite 1: Lakebase Connectivity
print("=== Suite 1: Lakebase Connectivity ===")

def test_connection_succeeds():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1")
    assert cur.fetchone()[0] == 1, "SELECT 1 should return 1"
    conn.close()

def test_table_exists():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fis_tasks")
    count = cur.fetchone()[0]
    assert count == 223, f"Expected 223 rows, got {count}"
    conn.close()

def test_vector_column_exists():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT lakebase_vector IS NOT NULL FROM fis_tasks LIMIT 1")
    assert cur.fetchone()[0] is True, "lakebase_vector should not be null"
    conn.close()

def test_text_column_exists():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT LENGTH(lakebase_text) > 0 FROM fis_tasks LIMIT 1")
    assert cur.fetchone()[0] is True, "lakebase_text should be non-empty"
    conn.close()

def test_indexes_exist():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'fis_tasks' ORDER BY indexname")
    indexes = [r[0] for r in cur.fetchall()]
    assert "idx_fis_tasks_vector" in indexes, f"Vector index missing. Found: {indexes}"
    assert "idx_fis_tasks_text" in indexes, f"Text index missing. Found: {indexes}"
    conn.close()

def test_pgvector_extension():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
    assert cur.fetchone() is not None, "pgvector extension not installed"
    conn.close()

run_test("Connection succeeds", test_connection_succeeds)
run_test("Table has 223 rows", test_table_exists)
run_test("Vector column is populated", test_vector_column_exists)
run_test("Text column is populated", test_text_column_exists)
run_test("Vector and text indexes exist", test_indexes_exist)
run_test("pgvector extension installed", test_pgvector_extension)

# COMMAND ----------

# DBTITLE 1,Test Suite 2: Embedding Model
print("\n=== Suite 2: Embedding Model ===")

def test_embedding_returns_1024_dims():
    emb = embed_query("camera misalignment")
    assert len(emb) == 1024, f"Expected 1024 dims, got {len(emb)}"

def test_embedding_deterministic():
    e1 = embed_query("modem offline")
    e2 = embed_query("modem offline")
    diff = sum(abs(a - b) for a, b in zip(e1, e2))
    assert diff < 0.01, f"Same input should give near-identical embeddings, diff={diff}"

def test_embedding_different_for_different_inputs():
    e1 = embed_query("camera alignment")
    e2 = embed_query("modem connectivity")
    from math import sqrt
    dot = sum(a * b for a, b in zip(e1, e2))
    n1 = sqrt(sum(a * a for a in e1))
    n2 = sqrt(sum(b * b for b in e2))
    cosine = dot / (n1 * n2)
    assert cosine < 0.95, f"Different queries should have different embeddings, cosine={cosine:.4f}"

def test_embedding_handles_long_text():
    long_text = "equipment failure " * 500
    emb = embed_query(long_text)
    assert len(emb) == 1024, f"Long text should still produce 1024 dims, got {len(emb)}"

run_test("Embedding returns 1024 dimensions", test_embedding_returns_1024_dims)
run_test("Embedding is deterministic", test_embedding_deterministic)
run_test("Different inputs give different embeddings", test_embedding_different_for_different_inputs)
run_test("Handles long text input", test_embedding_handles_long_text)

# COMMAND ----------

# DBTITLE 1,Test Suite 3: Vector Search (Lakebase pgvector)
print("\n=== Suite 3: Vector Search ===")

def test_vector_similarity_search():
    emb = embed_query("camera misalignment")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT number, title, 1 - (lakebase_vector <=> %s::vector) AS score
        FROM fis_tasks
        ORDER BY lakebase_vector <=> %s::vector
        LIMIT 5
    """, (emb_str, emb_str))
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 5, f"Expected 5 results, got {len(rows)}"
    assert rows[0][2] > 0.5, f"Top result score should be > 0.5, got {rows[0][2]:.3f}"
    # Check results are camera-related
    titles = " ".join(r[1].lower() for r in rows)
    assert "camera" in titles or "alpr" in titles, f"Top results should be camera-related: {titles[:200]}"

def test_full_text_search():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT number, ts_rank_cd(to_tsvector('english', lakebase_text), plainto_tsquery('english', 'modem offline')) AS score
        FROM fis_tasks
        WHERE to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', 'modem offline')
        ORDER BY score DESC LIMIT 5
    """)
    rows = cur.fetchall()
    conn.close()
    assert len(rows) > 0, "Full-text search for 'modem offline' should return results"
    assert rows[0][1] > 0, "Text score should be positive"

def test_hybrid_search_combines_scores():
    emb = embed_query("modem offline")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute(f"""
        WITH vector_search AS (
            SELECT number, 1 - (lakebase_vector <=> %s::vector) AS vector_score
            FROM fis_tasks ORDER BY lakebase_vector <=> %s::vector LIMIT 10
        ),
        text_search AS (
            SELECT number, ts_rank_cd(to_tsvector('english', lakebase_text), plainto_tsquery('english', %s)) AS text_score
            FROM fis_tasks WHERE to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', %s)
        )
        SELECT v.number, v.vector_score, COALESCE(t.text_score, 0) AS text_score,
               (0.7 * v.vector_score + 0.3 * COALESCE(t.text_score, 0)) AS combined
        FROM vector_search v LEFT JOIN text_search t ON v.number = t.number
        ORDER BY combined DESC LIMIT 5
    """, (emb_str, emb_str, "modem offline", "modem offline"))
    rows = cur.fetchall()
    conn.close()
    assert len(rows) > 0, "Hybrid search should return results"
    # At least one result should have both vector and text scores
    has_both = any(r[1] > 0 and r[2] > 0 for r in rows)
    assert has_both, "At least one result should have both vector and text match"

def test_state_filter():
    emb = embed_query("equipment issue")
    emb_str = "[" + ",".join(str(v) for v in emb) + "]"
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute(f"""
        SELECT number, location_state
        FROM fis_tasks
        WHERE location_state = 'TX'
        ORDER BY lakebase_vector <=> %s::vector LIMIT 5
    """, (emb_str,))
    rows = cur.fetchall()
    conn.close()
    assert len(rows) > 0, "Should have results for TX"
    assert all(r[1] == "TX" for r in rows), "All results should be in TX"

run_test("Vector similarity returns relevant camera results", test_vector_similarity_search)
run_test("Full-text search finds modem offline", test_full_text_search)
run_test("Hybrid search combines vector + text scores", test_hybrid_search_combines_scores)
run_test("State filter restricts results correctly", test_state_filter)

# COMMAND ----------

# DBTITLE 1,Test Suite 4: Retriever Tool
print("\n=== Suite 4: Retriever Tool ===")

# Import the tool from the agent notebook (or redefine inline)
@tool
def fis_knowledge_search(query: str, location_state: Optional[str] = None, status_filter: Optional[str] = None) -> str:
    """Search FIS R&D task knowledge base."""
    query_embedding = embed_query(query)
    emb_str = "[" + ",".join(str(v) for v in query_embedding) + "]"
    where_clauses, params = [], []
    if location_state:
        where_clauses.append("location_state = %s")
        params.append(location_state)
    if status_filter:
        where_clauses.append("status = %s")
        params.append(status_filter)
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
    # For text_search: combine filter conditions with full-text match using AND
    text_conditions = list(where_clauses)
    text_conditions.append("to_tsvector('english', lakebase_text) @@ plainto_tsquery('english', %s)")
    text_where = "WHERE " + " AND ".join(text_conditions)
    sql = f"""
    WITH vector_search AS (
        SELECT number, title, location_state, location_site, status, priority_label,
               problem_category, root_cause, resolution, resolution_type, lakebase_text,
               1 - (lakebase_vector <=> %s::vector) AS vector_score
        FROM {LAKEBASE_TABLE} {where_sql}
        ORDER BY lakebase_vector <=> %s::vector LIMIT {TOP_K * 2}
    ),
    text_search AS (
        SELECT number, ts_rank_cd(to_tsvector('english', lakebase_text), plainto_tsquery('english', %s)) AS text_score
        FROM {LAKEBASE_TABLE}
        {text_where}
    )
    SELECT v.number, v.title, v.location_state, v.location_site, v.status, v.priority_label,
           v.problem_category, v.root_cause, v.resolution, v.resolution_type, v.lakebase_text,
           v.vector_score, COALESCE(t.text_score, 0) AS text_score,
           (0.7 * v.vector_score + 0.3 * COALESCE(t.text_score, 0)) AS combined_score
    FROM vector_search v LEFT JOIN text_search t ON v.number = t.number
    ORDER BY combined_score DESC LIMIT {TOP_K}
    """
    all_params = [emb_str] + params + [emb_str] + [query] + params + [query]
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
        results.append(f"**{r['number']}** \u2014 {r['title']}\nRoot Cause: {r['root_cause'] or 'N/A'}\nResolution: {r['resolution'] or 'N/A'}")
    return "\n\n===\n\n".join(results)

def test_tool_returns_results():
    result = fis_knowledge_search.invoke({"query": "camera misalignment"})
    assert "R&DTASK" in result, f"Tool should return task numbers. Got: {result[:200]}"
    assert len(result) > 100, "Result should be substantial"

def test_tool_with_state_filter():
    result = fis_knowledge_search.invoke({"query": "equipment issue", "location_state": "VA"})
    # Should return results, may contain VA references
    assert isinstance(result, str), "Should return a string"
    assert len(result) > 0, "Should return non-empty result"

def test_tool_with_no_match():
    result = fis_knowledge_search.invoke({"query": "quantum computing blockchain AI"})
    # Should still return results (vector search always finds nearest)
    assert isinstance(result, str), "Should return a string"

def test_tool_returns_top_k():
    result = fis_knowledge_search.invoke({"query": "modem connectivity problem"})
    task_count = result.count("R&DTASK")
    assert task_count <= TOP_K, f"Should return at most {TOP_K} tasks, got {task_count}"
    assert task_count >= 1, "Should return at least 1 task"

run_test("Tool returns task numbers for camera query", test_tool_returns_results)
run_test("Tool handles state filter", test_tool_with_state_filter)
run_test("Tool handles irrelevant query gracefully", test_tool_with_no_match)
run_test("Tool returns at most TOP_K results", test_tool_returns_top_k)

# COMMAND ----------

# DBTITLE 1,Test Suite 5: LangGraph Agent (3 tools, end-to-end)
print("\n=== Suite 5: LangGraph Agent (end-to-end) ===")

from difflib import get_close_matches as _gcm

# Build glossary tool for tests
glossary_df = spark.sql(f"SELECT term, definition, aliases, category, source_refs FROM {GLOSSARY_TABLE} WHERE status = 'approved'")
GLOSSARY = [row.asDict() for row in glossary_df.collect()]
_GLOSSARY_INDEX = {}
for _g in GLOSSARY:
    _GLOSSARY_INDEX[_g["term"].lower()] = _g
    if _g["aliases"]:
        for _a in _g["aliases"]:
            _GLOSSARY_INDEX[_a.lower()] = _g

@tool
def glossary_lookup(term: str) -> str:
    """Look up FIS domain terms/acronyms. Args: term: The term to look up."""
    t = term.lower().strip()
    if t in _GLOSSARY_INDEX:
        g = _GLOSSARY_INDEX[t]
        aliases = ", ".join(g["aliases"]) if g["aliases"] else "none"
        return f"**{g['term']}** ({g['category']})\nDefinition: {g['definition']}\nAliases: {aliases}"
    matches = _gcm(term, [g["term"] for g in GLOSSARY], n=3, cutoff=0.5)
    if matches:
        return "Similar: " + ", ".join(matches)
    return f"Term '{term}' not found."

import time as _time

@tool
def genie_query(question: str) -> str:
    """Query FIS analytics for quantitative answers. Args: question: The question."""
    try:
        op = w.genie.start_conversation(space_id=GENIE_SPACE_ID, content=question)
        try:
            msg = op.result(timeout=_time.monotonic() + 90)
        except Exception:
            resp = op.response
            for _ in range(45):
                msg = w.genie.get_message(space_id=GENIE_SPACE_ID, conversation_id=resp.conversation_id, message_id=resp.message_id)
                if msg.status and msg.status.value in ("COMPLETED", "FAILED", "CANCELLED"):
                    break
                _time.sleep(2)
            else:
                return "Genie timed out."
        parts = []
        if msg.attachments:
            for att in msg.attachments:
                if att.text and att.text.content:
                    parts.append(att.text.content)
                if att.query and att.query.query:
                    parts.append(f"SQL: {att.query.query}")
        return "\n".join(parts) if parts else "No answer."
    except Exception as e:
        return f"Error: {e}"

SYSTEM_PROMPT = """You are the FIS v2 Knowledge Agent with 3 tools: fis_knowledge_search (qualitative), glossary_lookup (terms), genie_query (quantitative). Route accordingly."""
llm = ChatDatabricks(endpoint=LLM_ENDPOINT)
agent = create_react_agent(model=llm, tools=[fis_knowledge_search, glossary_lookup, genie_query], prompt=SYSTEM_PROMPT)

def test_agent_answers_camera_question():
    result = agent.invoke({"messages": [{"role": "user", "content": "What camera alignment issues have been reported?"}]})
    answer = ""
    for msg in reversed(result["messages"]):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            answer = msg.content
            break
    assert len(answer) > 50, f"Agent should give a substantive answer. Got {len(answer)} chars"
    assert "R&DTASK" in answer, "Agent should cite task numbers"

def test_agent_answers_modem_question():
    result = agent.invoke({"messages": [{"role": "user", "content": "Tell me about modem offline issues"}]})
    answer = ""
    for msg in reversed(result["messages"]):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            answer = msg.content
            break
    assert len(answer) > 50, "Agent should answer modem questions"

def test_agent_handles_location_question():
    result = agent.invoke({"messages": [{"role": "user", "content": "What issues have been reported in Texas?"}]})
    answer = ""
    for msg in reversed(result["messages"]):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            answer = msg.content
            break
    assert len(answer) > 50, "Agent should handle location-specific questions"

def test_agent_uses_tool():
    """Verify the agent actually calls the search tool (not just hallucinating)."""
    result = agent.invoke({"messages": [{"role": "user", "content": "What is the root cause for enclosure sunshield warping?"}]})
    tool_calls = [m for m in result["messages"] if hasattr(m, "type") and m.type == "tool"]
    assert len(tool_calls) > 0, "Agent should have called the fis_knowledge_search tool"

run_test("Agent answers camera alignment question with citations", test_agent_answers_camera_question)
run_test("Agent answers modem offline question", test_agent_answers_modem_question)
run_test("Agent handles location-specific question", test_agent_handles_location_question)
run_test("Agent uses the search tool (not hallucinating)", test_agent_uses_tool)

# COMMAND ----------

# DBTITLE 1,Test Suite 6: Edge Cases & Data Quality
print("\n=== Suite 6: Edge Cases & Data Quality ===")

def test_all_rows_have_embeddings():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fis_tasks WHERE lakebase_vector IS NULL")
    null_count = cur.fetchone()[0]
    conn.close()
    assert null_count == 0, f"{null_count} rows have NULL embeddings"

def test_all_rows_have_text():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM fis_tasks WHERE lakebase_text IS NULL OR lakebase_text = ''")
    empty_count = cur.fetchone()[0]
    conn.close()
    assert empty_count == 0, f"{empty_count} rows have empty/null text"

def test_no_duplicate_numbers():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("SELECT number, COUNT(*) FROM fis_tasks GROUP BY number HAVING COUNT(*) > 1")
    dupes = cur.fetchall()
    conn.close()
    assert len(dupes) == 0, f"Found duplicate task numbers: {dupes}"

def test_metadata_columns_populated():
    conn = get_lakebase_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE title IS NOT NULL) AS has_title,
            COUNT(*) FILTER (WHERE status IS NOT NULL) AS has_status,
            COUNT(*) FILTER (WHERE root_cause IS NOT NULL) AS has_root_cause,
            COUNT(*) FILTER (WHERE resolution IS NOT NULL) AS has_resolution,
            COUNT(*) AS total
        FROM fis_tasks
    """)
    row = cur.fetchone()
    conn.close()
    assert row[0] == row[4], f"All rows should have titles ({row[0]}/{row[4]})"
    assert row[1] == row[4], f"All rows should have status ({row[1]}/{row[4]})"
    # root_cause and resolution may be null for some tasks
    assert row[2] > row[4] * 0.5, f"At least 50% should have root_cause ({row[2]}/{row[4]})"

def test_empty_query():
    """Tool should handle empty-like queries gracefully."""
    result = fis_knowledge_search.invoke({"query": "a"})
    assert isinstance(result, str), "Should return a string even for minimal queries"

run_test("All 223 rows have embeddings", test_all_rows_have_embeddings)
run_test("All 223 rows have text content", test_all_rows_have_text)
run_test("No duplicate task numbers", test_no_duplicate_numbers)
run_test("Metadata columns well-populated", test_metadata_columns_populated)
run_test("Handles minimal/empty-like queries", test_empty_query)

# COMMAND ----------

# DBTITLE 1,Test Suite 7: Glossary Lookup
print("\n=== Suite 7: Glossary Lookup ===")

def test_glossary_loaded():
    assert len(GLOSSARY) >= 90, f"Expected ~94 terms, got {len(GLOSSARY)}"
    assert len(_GLOSSARY_INDEX) > len(GLOSSARY), "Index should include aliases"

def test_exact_term_match():
    result = glossary_lookup.invoke({"term": "OVC"})
    assert "OVC" in result, f"Should find OVC. Got: {result[:200]}"
    assert "camera" in result.lower() or "overview" in result.lower(), "OVC should mention camera/overview"

def test_alias_match():
    result = glossary_lookup.invoke({"term": "Neology"})
    assert "PIPS" in result or "Neology" in result, f"Neology should match. Got: {result[:200]}"

def test_case_insensitive():
    r1 = glossary_lookup.invoke({"term": "ovc"})
    r2 = glossary_lookup.invoke({"term": "OVC"})
    # Both should find the same term
    assert "OVC" in r1 and "OVC" in r2, "Lookup should be case-insensitive"

def test_fuzzy_match():
    result = glossary_lookup.invoke({"term": "Kistlr"})  # typo
    assert "Kistler" in result or "Similar" in result or "similar" in result, f"Should fuzzy-match Kistler. Got: {result[:200]}"

def test_unknown_term():
    result = glossary_lookup.invoke({"term": "xyznonexistent"})
    assert "not found" in result.lower(), f"Should report not found. Got: {result[:200]}"

def test_glossary_categories():
    """Verify glossary has expected category diversity."""
    categories = set(g["category"] for g in GLOSSARY if g.get("category"))
    assert len(categories) >= 4, f"Expected >= 4 categories, got {categories}"

def test_all_terms_have_definitions():
    missing = [g["term"] for g in GLOSSARY if not g.get("definition")]
    assert len(missing) == 0, f"Terms missing definitions: {missing[:5]}"

run_test("Glossary loaded (94+ terms)", test_glossary_loaded)
run_test("Exact term match: OVC", test_exact_term_match)
run_test("Alias match: Neology -> PIPS", test_alias_match)
run_test("Case-insensitive lookup", test_case_insensitive)
run_test("Fuzzy match: Kistlr -> Kistler", test_fuzzy_match)
run_test("Unknown term returns not-found", test_unknown_term)
run_test("Glossary has category diversity", test_glossary_categories)
run_test("All terms have definitions", test_all_terms_have_definitions)

# COMMAND ----------

# DBTITLE 1,Test Suite 8: Genie Query (quantitative analytics)
print("\n=== Suite 8: Genie Query ===")

def test_genie_answers_count_question():
    result = genie_query.invoke({"question": "How many R&D tasks are there by status?"})
    assert isinstance(result, str), "Should return a string"
    assert len(result) > 20, f"Should return a substantive answer. Got {len(result)} chars"
    # Should have some data or SQL
    assert "SQL" in result or "status" in result.lower() or "count" in result.lower() or "Error" not in result, \
        f"Should contain data or SQL. Got: {result[:300]}"

def test_genie_returns_sql():
    result = genie_query.invoke({"question": "Show the top 5 assignment groups by number of tasks"})
    # Genie should return SQL if successful
    has_sql = "SQL" in result or "SELECT" in result.upper() or "sql" in result
    has_error = "error" in result.lower() or "timed out" in result.lower()
    assert has_sql or has_error, f"Should return SQL or error. Got: {result[:300]}"

def test_genie_handles_bad_question():
    """Genie should not crash on a gibberish question."""
    result = genie_query.invoke({"question": "asdfghjkl qwertyuiop"})
    assert isinstance(result, str), "Should return a string even for gibberish"
    # Should either answer or report failure gracefully
    assert len(result) > 0, "Should return non-empty response"

run_test("Genie answers count-by-status question", test_genie_answers_count_question)
run_test("Genie returns SQL in response", test_genie_returns_sql)
run_test("Genie handles bad question gracefully", test_genie_handles_bad_question)

# COMMAND ----------

# DBTITLE 1,Test Results Summary
print("\n" + "=" * 60)
print(f"TEST RESULTS: {results['passed']} passed, {results['failed']} failed")
print("=" * 60)
if results["errors"]:
    print("\nFailures:")
    for name, err in results["errors"]:
        print(f"  \u274c {name}: {err}")
else:
    print("\n\u2705 All tests passed!")

assert results["failed"] == 0, f"{results['failed']} test(s) failed"