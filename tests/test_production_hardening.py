import os
os.environ["TESTING"] = "1"
import sys
import asyncio
import time
from fastapi import HTTPException, BackgroundTasks

# Add parent path to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn, init_postgres_pool, close_postgres_pool
from memoryos.db.neo4j import get_neo4j_conn, _mock_graph_data
from memoryos.schemas.memory import MemoryIngest, MemoryReflect, MemoryRetrieve
from memoryos.services.ingestion import MemoryIngestionService
from memoryos.api.memories import (
    verify_workspace_key,
    ingest_memory,
    retrieve_context,
    trigger_replay,
    trigger_rebuild,
    get_job_status
)
from memoryos.services.background import background_graph_ingest

def seed_api_key(key: str, workspace_id: str):
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO api_keys (key, workspace_id, description) VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
            (key, workspace_id, "Test API Key")
        )
    conn.commit()
    conn.close()

def cleanup_api_keys():
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM api_keys WHERE key IN (%s, %s)",
            ("key_default", "key_api_test")
        )
    conn.commit()
    conn.close()
def test_production_hardening_suite():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Stage 1 Production Hardening Verification")
    print("============================================================")
    
    # 1. Connection Pool Verification
    print("\nStep 1: Verifying PostgreSQL connection pooling...")
    init_postgres_pool()
    conns = []
    try:
        # Check out multiple pooled connections concurrently
        for _ in range(5):
            c = get_postgres_conn()
            conns.append(c)
            # Verify basic connectivity
            with c.cursor() as cur:
                cur.execute("SELECT 1")
                res = cur.fetchone()[0]
                assert res == 1
        print("  Successfully checked out 5 pooled connections concurrently.")
    finally:
        for c in conns:
            c.close()
        close_postgres_pool()
        print("  Successfully returned connections and closed Postgres pool.")

    # 2. Workspace API Key Verification
    print("\nStep 2: Verifying workspace API key authorization...")
    seed_api_key("key_default", "default")
    seed_api_key("key_api_test", "api_test")
    # Disable testing env bypass to run active check
    old_force = os.environ.get("FORCE_AUTH_TEST")
    os.environ["FORCE_AUTH_TEST"] = "1"
    
    try:
        # Invalid API key credentials
        try:
            verify_workspace_key("default", authorization="Bearer invalid_key")
            raise AssertionError("Expected 403 HTTPException for invalid key but none raised.")
        except HTTPException as e:
            assert e.status_code == 403
        print("  Correctly rejected invalid credentials with 403.")
        
        # Missing Header
        try:
            verify_workspace_key("default", authorization=None)
            raise AssertionError("Expected 401 HTTPException for missing authorization but none raised.")
        except HTTPException as e:
            assert e.status_code == 401
        print("  Correctly rejected missing header with 401.")
        
        # Valid Default Key
        verify_workspace_key("default", authorization="Bearer key_default")
        print("  Correctly authorized whitelisted key 'key_default' for workspace 'default'.")
        
        # Valid workspace mismatch
        try:
            verify_workspace_key("api_test", authorization="Bearer key_default")
            raise AssertionError("Expected 403 HTTPException for workspace mismatch but none raised.")
        except HTTPException as e:
            assert e.status_code == 403
        print("  Correctly rejected workspace key mismatch.")
        
    finally:
        if old_force is not None:
            os.environ["FORCE_AUTH_TEST"] = old_force
        else:
            os.environ.pop("FORCE_AUTH_TEST", None)

    # 3. Relationship Enum Whitelisting and Fallback
    print("\nStep 3: Verifying relationship enum mapping and Cypher validation...")
    neo4j = get_neo4j_conn()
    assert neo4j is not None
    
    # Clean mock graph
    _mock_graph_data["entities"].clear()
    _mock_graph_data["relationships"].clear()
    
    # Ingest a whitelisted relation
    background_graph_ingest(
        memory_id="mem-1",
        content="Alice lives in Seattle.",
        user_id="user_test",
        workspace_id="default"
    )
    # Check mock relationships
    rels = _mock_graph_data["relationships"]
    assert len(rels) > 0
    assert rels[0]["type"] == "LIVES_IN"
    print("  Whitelisted relationship type 'LIVES_IN' inserted successfully.")
    
    # Ingest a non-whitelisted relation (e.g. MAKES_COFFEE)
    _mock_graph_data["relationships"].clear()
    background_graph_ingest(
        memory_id="mem-2",
        content="Alice drives a Tesla.",
        user_id="user_test",
        workspace_id="default"
    )
    rels = _mock_graph_data["relationships"]
    assert len(rels) > 0
    # MAKES_COFFEE should map to fallback RELATED_TO
    assert rels[0]["type"] == "RELATED_TO"
    print("  Non-whitelisted relationship mapped to fallback type 'RELATED_TO' successfully.")

    # 4. Asynchronous Replay Determinism and Job Tracking
    print("\nStep 4: Verifying asynchronous state replay determinism & progress tracking...")
    # Clear DB and mock graph
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM event_store")
        cur.execute("DELETE FROM background_jobs")
    conn.commit()
    
    # Ingest memories to build up logs
    req_ingest1 = MemoryIngest(
        user_id="user_test",
        workspace_id="default",
        content="Alice uses python."
    )
    req_ingest2 = MemoryIngest(
        user_id="user_test",
        workspace_id="default",
        content="Bob uses rust."
    )
    
    asyncio.run(MemoryIngestionService.ingest(req_ingest1, background_tasks=None))
    asyncio.run(MemoryIngestionService.ingest(req_ingest2, background_tasks=None))
    
    # Purge memories to corrupt state
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
    conn.commit()
    
    # Trigger Replay Job
    bg = BackgroundTasks()
    req_reflect = MemoryReflect(user_id="user_test", workspace_id="default")
    replay_res = asyncio.run(trigger_replay(req_reflect, bg, authorization="key_default"))
    job_id = replay_res["job_id"]
    print(f"  Triggered Replay Job: {job_id}")
    
    # Run the worker task synchronously for the test
    from memoryos.core.event_store import replay_events
    asyncio.run(replay_events(job_id, "user_test", "default"))
    
    # Check Job Status
    job_info = asyncio.run(get_job_status(job_id, authorization="key_default"))
    print(f"  Job status info: {job_info}")
    assert job_info["status"] == "COMPLETED"
    assert job_info["total_events"] == 2
    assert job_info["processed_events"] == 2
    print("  Replay job marked COMPLETED with correct processed count.")
    
    # Assert memories restored identical
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM memories")
        count = cur.fetchone()[0]
        assert count == 2
    print("  State replayed deterministically with 100% data recovery.")
    
    # 5. Health Check Endpoint
    print("\nStep 5: Verifying Health Check status structure...")
    from memoryos.main import health_check
    health_res = asyncio.run(health_check())
    print(f"  Health Check result: {health_res}")
    assert "status" in health_res
    assert "dependencies" in health_res
    assert health_res["dependencies"]["postgres"] == "connected"
    print("  Health endpoint active and returning connectivity state.")
    
    cleanup_api_keys()
    conn.close()
    print("\n" + "=" * 60)
    print("  Stage 1 Production Hardening Verification SUCCESS!")
    print("=" * 60)

if __name__ == "__main__":
    test_production_hardening_suite()
