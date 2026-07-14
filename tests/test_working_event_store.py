import os
import sys
import asyncio
from datetime import datetime, timezone

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.api.memories import (
    update_working_memory,
    get_working_memory,
    ingest_memory,
    retrieve_context,
    trigger_state_replay
)
from memoryos.schemas.memory import (
    WorkingMemoryUpdate,
    MemoryIngest,
    MemoryRetrieve,
    MemoryReflect
)

def test_working_event_store_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Working Memory & Event Store Tests")
    print("============================================================")
    
    # 1. Reset Database State
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_logs")
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM episodes")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM workflows")
        cur.execute("DELETE FROM event_store")
        cur.execute("DELETE FROM users")
    conn.commit()
    
    neo4j = get_neo4j_conn()
    is_mock = getattr(neo4j, "is_mock", False)
    
    if is_mock:
        _mock_graph_data["entities"].clear()
        _mock_graph_data["relationships"].clear()
    else:
        neo4j.query("MATCH (n) DETACH DELETE n")
        
    user_id = "usr_alice"
    workspace_id = "default"
    
    # Pre-populate user
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
    conn.commit()
    
    # 2. Test Working Memory updates & queries
    print("\nStep 1: Updating Working Memory register...")
    req_wm_up = WorkingMemoryUpdate(
        user_id=user_id,
        workspace_id=workspace_id,
        current_goal="Build deployment script",
        constraints=["Limit budget to 50 USD", "Use SQLite fallback"],
        current_plan=["Create schemas", "Write ingestion script"],
        scratchpad="Working memory integration test active"
    )
    res_wm_up = asyncio.run(update_working_memory(req_wm_up))
    assert res_wm_up.current_goal == "Build deployment script"
    assert len(res_wm_up.constraints) == 2
    
    print("Step 2: Fetching active Working Memory register...")
    res_wm_get = asyncio.run(get_working_memory(user_id, workspace_id))
    assert res_wm_get.scratchpad == "Working memory integration test active"
    assert res_wm_get.current_plan[0] == "Create schemas"
    
    # 3. Verify retrieve_context displays active working memory
    print("Step 3: Checking retrieval context for Working Memory display...")
    req_ret = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="What should I build next?",
        limit=5
    )
    res_ret = asyncio.run(retrieve_context(req_ret, format="markdown"))
    markdown_context = res_ret["markdown"]
    print("\n  Markdown Context returned:")
    print(markdown_context)
    
    assert "## Active Working Memory" in markdown_context
    assert "- **Goal**: Build deployment script" in markdown_context
    assert "- **Constraints**: Limit budget to 50 USD, Use SQLite fallback" in markdown_context
    assert "- **Scratchpad**: Working memory integration test active" in markdown_context
    
    # 4. Test Event Store Logging
    print("\nStep 4: Ingesting memories and verifying Event Store logs...")
    req_mem1 = MemoryIngest(
        user_id=user_id,
        workspace_id=workspace_id,
        content="Alice codes in Rust."
    )
    req_mem2 = MemoryIngest(
        user_id=user_id,
        workspace_id=workspace_id,
        content="Bob builds Docker containers."
    )
    
    # Ingest in background/foreground
    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    asyncio.run(ingest_memory(req_mem1, bg))
    asyncio.run(ingest_memory(req_mem2, bg))
    
    # Verify count of event logs
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM event_store WHERE user_id = %s", (user_id,))
        evt_cnt = cur.fetchone()[0]
        print(f"  Logged events count: {evt_cnt}")
        assert evt_cnt == 2, f"Expected 2 events logged, found {evt_cnt}"
        
        # Verify count of memories in DB
        cur.execute("SELECT count(*) FROM memories WHERE user_id = %s", (user_id,))
        mem_cnt = cur.fetchone()[0]
        print(f"  Memories in DB: {mem_cnt}")
        assert mem_cnt == 2, f"Expected 2 memories in database, found {mem_cnt}"
        
    # 5. Simulate corruption/purge and replay
    print("\nStep 5: Simulating database corruption (wiping derived records)...")
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM episodes")
    conn.commit()
    
    # Verify count of memories is 0
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories")
        mem_cnt = cur.fetchone()[0]
        print(f"  Memories in DB after corruption: {mem_cnt}")
        assert mem_cnt == 0
        
    print("Step 6: Triggering state replay from event logs...")
    req_replay = MemoryReflect(
        user_id=user_id,
        workspace_id=workspace_id
    )
    res_replay = asyncio.run(trigger_state_replay(req_replay))
    print(f"  Replay response: {res_replay}")
    assert res_replay["status"] == "success"
    
    # Verify that memories are restored
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories")
        mem_cnt_after = cur.fetchone()[0]
        print(f"  Memories in DB after state replay: {mem_cnt_after}")
        assert mem_cnt_after == 2, f"Expected 2 memories restored, found {mem_cnt_after}"
        
        cur.execute("SELECT content FROM memories ORDER BY created_at ASC")
        rows = cur.fetchall()
        contents = [r[0] for r in rows]
        print(f"  Restored contents: {contents}")
        assert "Alice codes in Rust." in contents
        assert "Bob builds Docker containers." in contents
        
    conn.close()
    print("\n" + "=" * 60)
    print("  Working Memory & Event Store tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_working_event_store_system()
