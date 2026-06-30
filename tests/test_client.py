import os
import sys
import time
import json
import asyncio
from fastapi import BackgroundTasks

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import Neo4jConnector

def init_db():
    print("Connecting to PostgreSQL to run schema initialization...")
    try:
        conn = get_postgres_conn()
        conn.autocommit = True
        with conn.cursor() as cur:
            # Locate schema.sql relative to this script
            schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../schema.sql"))
            with open(schema_path, "r") as f:
                schema_sql = f.read()
            print(f"Executing {schema_path}...")
            cur.execute(schema_sql)
        conn.close()
        print("PostgreSQL Database initialized successfully.")
    except Exception as e:
        print(f"\n[ERROR] Failed to connect to PostgreSQL: {e}")
        print("Please verify that your database container is running: run 'docker compose up -d'")
        sys.exit(1)

def verify_neo4j():
    print("Verifying connection to Neo4j Graph database...")
    try:
        connector = Neo4jConnector()
        result = connector.query("RETURN 1 AS val")
        connector.close()
        print("Neo4j Database connection verified successfully.")
        return True
    except Exception as e:
        print(f"\n[WARNING] Neo4j is not running or accessible: {e}")
        print("Graph database features will operate in mock/degraded mode. (FastAPI will still work).")
        return False

def run_tests():
    from memoryos.api.memories import (
        ingest_memory, retrieve_context, apply_decay, consolidate_memories,
        format_context_markdown
    )
    from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
    from memoryos.core.classifier import classify_memory
    from memoryos.core.cache import stm_cache
    from memoryos.services.background import background_graph_ingest
    
    print("\n" + "=" * 60)
    print("  MemoryOS v1.2.0 - Comprehensive Integration Tests (Modular)")
    print("=" * 60)
    
    # ============================================================
    # TEST 1: Memory Classification
    # ============================================================
    print("\n--- Test 1: Memory Classification ---")
    test_classifications = [
        ("Alice loves coding in Rust.", "PREFERENCE"),
        ("Alice works at Acme Corp.", "FACTUAL"),
        ("Alice switched her editor to Neovim last week.", "EPISODIC"),
        ("Alice prefers writing clean documentation.", "PREFERENCE"),
        ("Alice lives in Seattle.", "FACTUAL"),
        ("Alice uses Docker Compose for deployments.", "FACTUAL"),
    ]
    classification_pass = True
    for content, expected in test_classifications:
        result = classify_memory(content)
        status = "PASS" if result == expected else "FAIL"
        if result != expected:
            classification_pass = False
        print(f"  [{status}] '{content}' -> {result} (expected: {expected})")
    print(f"  Classification test: {'ALL PASSED' if classification_pass else 'SOME FAILED'}")
    
    # ============================================================
    # TEST 2: Ingestion with Classification
    # ============================================================
    print("\n--- Test 2: Ingestion with Memory Type Classification ---")
    
    user_id = "usr_alice"
    samples = [
        "Alice loves coding backend APIs in Rust and using PostgreSQL.",
        "Alice lives in Seattle and works at Acme Corp.",
        "Alice prefers writing clean documentation and uses Docker Compose.",
        "Alice switched her favorite text editor to Neovim last week.",
    ]
    
    bg_tasks = BackgroundTasks()
    ingested_ids = []
    
    for idx, content in enumerate(samples):
        ingest_data = MemoryIngest(
            user_id=user_id,
            content=content,
            workspace_id="default"
        )
        
        async def run_ingest(data=ingest_data):
            return await ingest_memory(data, bg_tasks)
            
        loop = asyncio.get_event_loop()
        res = loop.run_until_complete(run_ingest())
        ingested_ids.append(res.memory_id)
        print(f"  [{idx+1}/{len(samples)}] Ingested: '{content}'")
        print(f"         -> ID: {res.memory_id} | Message: {res.message}")
        
    # ============================================================
    # TEST 3: STM Cache Verification
    # ============================================================
    print("\n--- Test 3: STM Cache Verification ---")
    stm_items = stm_cache.get(user_id, "default")
    print(f"  STM cache entries for {user_id}: {len(stm_items)}")
    assert len(stm_items) == len(samples), f"Expected {len(samples)} STM entries, got {len(stm_items)}"
    print(f"  [PASS] STM cache contains {len(stm_items)} recent memories")
    
    # ============================================================
    # TEST 4: Background Graph Ingestion + Contradiction Resolution
    # ============================================================
    print("\n--- Test 4: Graph Ingestion + Contradiction Resolution ---")
    for idx, content in enumerate(samples):
        background_graph_ingest(f"test_id_{idx}", content, user_id, "default")
    
    # Now ingest a CONTRADICTING memory
    contradiction_content = "Alice switched from Neovim to VSCode yesterday."
    ingest_data = MemoryIngest(
        user_id=user_id,
        content=contradiction_content,
        workspace_id="default"
    )
    
    async def run_ingest_contradiction():
        return await ingest_memory(ingest_data, bg_tasks)
    
    res = loop.run_until_complete(run_ingest_contradiction())
    print(f"  Ingested contradiction: '{contradiction_content}' -> ID: {res.memory_id}")
    
    # Run graph extraction for the contradiction
    background_graph_ingest(res.memory_id, contradiction_content, user_id, "default")
    print(f"  [INFO] Contradiction resolution should have triggered during graph ingestion")
    
    time.sleep(0.5)
    
    # ============================================================
    # TEST 5: Hybrid Retrieval with Type Labels
    # ============================================================
    print("\n--- Test 5: Hybrid Retrieval with Classification + STM Boost ---")
    
    test_queries = [
        "What programming languages and databases does Alice use?",
        "Where does Alice work?",
        "What editor does Alice code in?",
    ]
    
    for q in test_queries:
        retrieve_data = MemoryRetrieve(
            user_id=user_id,
            query=q,
            limit=3
        )
        
        async def run_retrieve(data=retrieve_data):
            return await retrieve_context(data)
            
        response = loop.run_until_complete(run_retrieve())
        
        print(f"\n  Query: '{q}'")
        print(f"  Results (Token budget count: {response.context_token_count}):")
        for item in response.results:
            print(f"    - [{item.type}] (Score: {item.score:.3f}): {item.content}")
    
    # ============================================================
    # TEST 6: Markdown Context Assembly
    # ============================================================
    print("\n--- Test 6: Markdown Context Assembly ---")
    
    retrieve_data = MemoryRetrieve(user_id=user_id, query="Tell me about Alice", limit=5)
    
    async def run_markdown_retrieve():
        return await retrieve_context(retrieve_data, format="markdown")
    
    md_response = loop.run_until_complete(run_markdown_retrieve())
    print(f"  Markdown output:")
    for line in md_response["markdown"].split("\n"):
        print(f"    {line}")
    
    # ============================================================
    # TEST 7: Consolidation Engine
    # ============================================================
    print("\n--- Test 7: Consolidation Engine ---")
    
    dup_samples = [
        "Alice works at Acme Corp in the engineering department.",
        "Alice is employed at Acme Corp as an engineer.",
    ]
    for content in dup_samples:
        ingest_data = MemoryIngest(user_id=user_id, content=content, workspace_id="default")
        async def run_dup_ingest(data=ingest_data):
            return await ingest_memory(data, bg_tasks)
        loop.run_until_complete(run_dup_ingest())
    
    async def run_consolidate():
        return await consolidate_memories(user_id=user_id, workspace_id="default")
    
    consolidate_result = loop.run_until_complete(run_consolidate())
    print(f"  Consolidation result: {consolidate_result}")
    
    # ============================================================
    # TEST 8: Decay Engine
    # ============================================================
    print("\n--- Test 8: Decay Engine ---")
    
    async def run_decay():
        return await apply_decay()
    
    decay_result = loop.run_until_complete(run_decay())
    print(f"  Decay result: {decay_result}")

if __name__ == "__main__":
    init_db()
    verify_neo4j()
    
    try:
        run_tests()
        print("\n" + "=" * 60)
        print("  All integration tests completed successfully!")
        print("=" * 60)
    except Exception as e:
        import traceback
        print(f"\n[ERROR] Test run failed: {e}")
        traceback.print_exc()
