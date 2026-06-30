import os
import sys
import time
import asyncio
from datetime import datetime, timezone, timedelta
from fastapi import BackgroundTasks

# Ensure package path is registered
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import ingest_memory, retrieve_context, consolidate_memories
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.core.cache import stm_cache
from memoryos.db.postgres import get_postgres_conn, MockPostgresConnection
from memoryos.db.neo4j import Neo4jConnector

def init_clean_db():
    conn = get_postgres_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../schema.sql"))
            with open(schema_path, "r") as f:
                schema_sql = f.read()
            # If SQLite, run clean split queries
            if isinstance(conn, MockPostgresConnection):
                from memoryos.db.postgres import MockCursor
                mock_cur = MockCursor(conn.sqlite_conn.cursor())
                for stmt in schema_sql.split(";"):
                    if stmt.strip():
                        mock_cur.execute(stmt)
            else:
                cur.execute(schema_sql)
        except Exception as e:
            print(f"[Setup Warning] DB Schema run: {e}")
            
    # Purge tables
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")
    conn.close()
    
    # Reset STMCache
    stm_cache._store = {}
    
    # Purge Neo4j if available
    try:
        neo4j = Neo4jConnector()
        neo4j.query("MATCH (n) DETACH DELETE n")
        neo4j.close()
    except Exception:
        pass

async def evaluate_accuracy():
    print("=" * 70)
    print("  MemoryOS v1.2.0 - Comprehensive Accuracy & Recall Evaluation")
    print("=" * 70)
    
    user_id = "eval_user"
    workspace_id = "eval_workspace"
    bg_tasks = BackgroundTasks()
    
    # Clean setup
    init_clean_db()
    
    # ------------------------------------------------------------------
    # SCENARIO 1: Precision@K and Recall@K under Heavy Distractor Noise
    # ------------------------------------------------------------------
    print("\n[Scenario 1] Testing Recall & Precision under Distractor Noise...")
    
    # Ingest 10 target memories
    target_memories = [
        "Dave hates mushrooms on pizza.",
        "Dave's primary laptop is a MacBook Pro M3.",
        "Dave worked at Google from 2019 to 2022.",
        "Dave's wife is named Sarah.",
        "Dave prefers writing backend logic using FastAPI.",
        "Dave lives in Vancouver, Canada.",
        "Dave speaks English and fluent Spanish.",
        "Dave is allergic to peanuts.",
        "Dave's favorite coffee roast is light roast Ethiopian.",
        "Dave uses GitKraken for visual Git branching."
    ]
    
    # Ingest 100 random noise distractor memories
    distractor_noise = [
        "The weather in London is frequently overcast.",
        "Python 3.12 introduces syntax improvements for typing.",
        "A standard soccer field is about 100 meters long.",
        "Many software developers prefer mechanical keyboards.",
        "Docker containers isolate application processes.",
        "Kubernetes orchestrates container scaling.",
        "Database indexes speed up read query times.",
        "Regular physical exercise improves cardiovascular health.",
        "JavaScript remains the dominant language for front-end web apps.",
        "A cup of tea contains less caffeine than espresso."
    ]
    
    # Load targets
    print(f"  Ingesting {len(target_memories)} high-value targets...")
    for text in target_memories:
        await ingest_memory(MemoryIngest(user_id=user_id, content=text, workspace_id=workspace_id), bg_tasks)
        
    # Load distractors
    num_distractors = 100
    print(f"  Ingesting {num_distractors} distractor noise memories...")
    for i in range(num_distractors):
        text = f"{distractor_noise[i % len(distractor_noise)]} Random ID: {i}"
        await ingest_memory(MemoryIngest(user_id=user_id, content=text, workspace_id=workspace_id), bg_tasks)
        
    # Evaluate queries
    evaluation_queries = [
        ("What does Dave dislike on his pizza?", "mushrooms"),
        ("What kind of laptop does Dave use?", "MacBook Pro"),
        ("Where did Dave work before?", "Google"),
        ("What is Dave's spouse's name?", "Sarah"),
        ("What backend framework does Dave code in?", "FastAPI"),
        ("Where does Dave reside?", "Vancouver"),
        ("What languages does Dave speak?", "Spanish"),
        ("What foods is Dave allergic to?", "peanuts"),
        ("What coffee does Dave like?", "Ethiopian"),
        ("What Git client does Dave use?", "GitKraken")
    ]
    
    hits = 0
    total_latency_ms = 0.0
    
    for query_text, expected_keyword in evaluation_queries:
        t0 = time.time()
        res = await retrieve_context(MemoryRetrieve(user_id=user_id, query=query_text, limit=3, workspace_id=workspace_id))
        total_latency_ms += (time.time() - t0) * 1000
        
        # Check if expected target text matches any of the top 3 results
        retrieved_texts = " ".join([item.content for item in res.results]).lower()
        if expected_keyword.lower() in retrieved_texts:
            hits += 1
            print(f"  [SUCCESS] Query: '{query_text}' -> Found keyword '{expected_keyword}'")
        else:
            print(f"  [FAILED]  Query: '{query_text}' -> Missing keyword '{expected_keyword}'")
            print(f"            Top retrieved: {[r.content for r in res.results]}")
            
    recall_rate = hits / len(evaluation_queries)
    avg_latency = total_latency_ms / len(evaluation_queries)
    print(f"\n  => Scenario 1 Metrics: Recall@3 = {recall_rate * 100:.1f}%, Avg Latency = {avg_latency:.2f}ms")
    
    # ------------------------------------------------------------------
    # SCENARIO 2: Preference contradiction accuracy (Temporal Correctness)
    # ------------------------------------------------------------------
    print("\n[Scenario 2] Testing Contradiction Resolution & Temporal Preference Accuracy...")
    
    # Ingest baseline preference
    baseline_res = await ingest_memory(MemoryIngest(user_id=user_id, content="Bob works at Microsoft.", workspace_id=workspace_id), bg_tasks)
    
    # Execute graph ingestion manually
    from memoryos.services.background import background_graph_ingest
    background_graph_ingest(baseline_res.memory_id, "Bob works at Microsoft.", user_id, workspace_id)
    
    # Ingest newer conflicting memory
    contradiction_res = await ingest_memory(
        MemoryIngest(user_id=user_id, content="Bob works at Google now, leaving Microsoft.", workspace_id=workspace_id),
        bg_tasks
    )
    
    # Execute graph contradiction resolution manually
    background_graph_ingest(contradiction_res.memory_id, "Bob works at Google now, leaving Microsoft.", user_id, workspace_id)
    
    # Query for the latest fact
    res = await retrieve_context(MemoryRetrieve(user_id=user_id, query="Where does Bob work?", limit=3, workspace_id=workspace_id))
    retrieved_text = " ".join([r.content for r in res.results])
    
    has_new = "google" in retrieved_text.lower()
    has_old = "microsoft" in retrieved_text.lower()
    
    print(f"  Retrieved context for Bob's employment: {[r.content for r in res.results]}")
    if has_new and not has_old:
        print("  [SUCCESS] Bob's old preference was deactivated and new preference correctly returned.")
        scenario_2_pass = True
    else:
        print("  [FAILED] Contradictory old preference still present in retrieved context.")
        scenario_2_pass = False
        
    # ------------------------------------------------------------------
    # SCENARIO 3: Recency and Frequency decay correctness
    # ------------------------------------------------------------------
    print("\n[Scenario 3] Testing Score Decay and Retrieval Filtering...")
    
    # Ingest a memory to decay
    decay_text = "Dave visited Berlin in 2021."
    decay_res = await ingest_memory(MemoryIngest(user_id=user_id, content=decay_text, workspace_id=workspace_id), bg_tasks)
    
    # Manually simulate historical decay inside DB (accessed 100 days ago)
    conn = get_postgres_conn()
    hundred_days_ago = datetime.now(timezone.utc) - timedelta(days=100)
    with conn.cursor() as cur:
        # Update last accessed and decrease static importance
        cur.execute(
            "UPDATE memories SET last_accessed_at = %s, importance_score = 0.1 WHERE id = %s",
            (hundred_days_ago, decay_res.memory_id)
        )
    conn.commit()
    conn.close()
    
    # Apply decay sweep
    from memoryos.api.memories import apply_decay
    decay_sweep = await apply_decay()
    print(f"  Applied decay sweep. Archived memories count: {decay_sweep['archived_count']}")
    
    # Verify that the decayed memory is no longer returned
    res = await retrieve_context(MemoryRetrieve(user_id=user_id, query="Where did Dave travel to in 2021?", limit=3, workspace_id=workspace_id))
    retrieved_text = " ".join([r.content for r in res.results])
    
    if "berlin" not in retrieved_text.lower():
        print("  [SUCCESS] Decayed memory successfully pruned and filtered from active context.")
        scenario_3_pass = True
    else:
        print("  [FAILED] Decayed memory was still returned in retrieval context.")
        scenario_3_pass = False

    # ------------------------------------------------------------------
    # ACCURACY REPORT CARD
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  MemoryOS Accuracy Evaluation Report Card")
    print("=" * 70)
    print(f"  1. Recall@3 under noise:      {recall_rate * 100:.1f}% (Target: > 90%)")
    print(f"  2. Contradiction Accuracy:     {'PASS' if scenario_2_pass else 'FAIL'}")
    print(f"  3. Temporal Decay Accuracy:    {'PASS' if scenario_3_pass else 'FAIL'}")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(evaluate_accuracy())
