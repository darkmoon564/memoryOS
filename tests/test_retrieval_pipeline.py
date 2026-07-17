import os
import sys

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.services.background import background_graph_ingest
from memoryos.core.retrieval_planner import plan_retrieval
from memoryos.core.graph_expander import expand_entities
from memoryos.core.episodes import process_conversation_log
from memoryos.models.embeddings import get_embedding_model
from memoryos.api.memories import retrieve_context
from memoryos.schemas.memory import MemoryRetrieve

def test_retrieval_pipeline():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Retrieval Pipeline Restructuring Tests")
    print("============================================================")
    
    # 1. Reset Database State
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_logs")
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM episodes")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")
    conn.commit()
    
    neo4j = get_neo4j_conn()
    is_mock = getattr(neo4j, "is_mock", False)
    
    if is_mock:
        print("[INFO] Operating in Neo4j Mock Mode. Resetting mock data...")
        _mock_graph_data["entities"].clear()
        _mock_graph_data["relationships"].clear()
        _mock_graph_data.setdefault("aliases", {}).clear()
    else:
        print("[INFO] Operating in Real Neo4j Mode. Purging database...")
        neo4j.query("MATCH (n) DETACH DELETE n")
        
    user_id = "usr_alice"
    session_id = "sess_1"
    workspace_id = "default"
    
    # Pre-populate user and session
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        cur.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (session_id, user_id))
    conn.commit()
    
    # 2. Ingest structured facts
    from fastapi import BackgroundTasks
    from memoryos.schemas.memory import MemoryIngest
    from memoryos.api.memories import ingest_memory
    import asyncio
    
    print("\nStep 1: Ingesting raw facts via API...")
    bg_tasks = BackgroundTasks()
    ingest_data = MemoryIngest(
        user_id=user_id,
        content="Alice works at Acme Corp.",
        workspace_id=workspace_id,
        session_id=session_id
    )
    
    # Run the ingestion end-to-end
    loop = asyncio.get_event_loop()
    res = loop.run_until_complete(ingest_memory(ingest_data, bg_tasks))
    print(f"  Ingested memory ID: {res.memory_id}")
    
    # Manually run enqueued background tasks synchronously so graph updates are committed
    for task in bg_tasks.tasks:
        task.func(*task.args, **task.kwargs)
    
    # 3. Test Retrieval Planner
    print("\nStep 2: Testing Retrieval Planner...")
    plan1 = plan_retrieval("Where does Alice work?")
    print(f"  Plan 1 (Where does Alice work?): {plan1}")
    assert "alice" in plan1["entities"], "Expected 'alice' in entities"
    assert plan1["intent"] == "factual_lookup", "Expected factual_lookup intent"
    
    plan2 = plan_retrieval("Bob prefers coding in Rust.")
    print(f"  Plan 2 (Bob prefers coding in Rust.): {plan2}")
    assert plan2["intent"] == "preference_query", "Expected preference_query intent"
    
    # 4. Test Entity Graph Expansion
    print("\nStep 3: Testing Entity Graph Expansion...")
    expansion = expand_entities(neo4j, user_id, workspace_id, ["alice"])
    print(f"  Expansion: {expansion}")
    assert "alice" in expansion["expanded_entities"], "Expected 'alice' in expanded entities"
    assert any("acme corp" in ent for ent in expansion["expanded_entities"]), "Expected 'acme corp' in expanded entities"
    assert len(expansion["graph_facts"]) > 0, "Expected at least 1 graph fact"
    
    # 5. Test End-to-End retrieve_context API function
    print("\nStep 4: Testing retrieve_context hybrid search...")
    ret_req = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="Where does Alice work?",
        limit=5
    )
    
    import asyncio
    results = asyncio.run(retrieve_context(ret_req))
    
    print("\n  Retrieval Results:")
    for idx, r in enumerate(results.results):
        print(f"    [{idx + 1}] Type: {r.type}, Score: {r.score:.3f}, Content: '{r.content}'")
        
    # Assertions
    types = [r.type for r in results.results]
    assert "FACTUAL" in types, "Expected a FACTUAL memory matching Alice's workplace"
    assert "GRAPH_FACT" in types, "Expected a GRAPH_FACT memory from graph expansion"
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Retrieval Pipeline tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    from unittest.mock import patch
    with patch("memoryos.api.memories.verify_workspace_key", return_value=None):
        test_retrieval_pipeline()
