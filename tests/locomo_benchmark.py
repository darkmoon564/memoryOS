import os
import sys
import time
import asyncio
import statistics
from fastapi import BackgroundTasks

# Register package path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import ingest_memory, retrieve_context
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.services.background import background_graph_ingest
from memoryos.core.cache import stm_cache
from memoryos.db.postgres import get_postgres_conn, MockPostgresConnection
from memoryos.db.neo4j import Neo4jConnector

def reset_db():
    conn = get_postgres_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../schema.sql"))
            with open(schema_path, "r") as f:
                schema_sql = f.read()
            if isinstance(conn, MockPostgresConnection):
                from memoryos.db.postgres import MockCursor
                mock_cur = MockCursor(conn.sqlite_conn.cursor())
                for stmt in schema_sql.split(";"):
                    if stmt.strip():
                        mock_cur.execute(stmt)
            else:
                cur.execute(schema_sql)
        except Exception as e:
            pass
            
    with conn.cursor() as cur:
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")
    conn.close()
    
    stm_cache._store = {}
    
    try:
        neo4j = Neo4jConnector()
        neo4j.query("MATCH (n) DETACH DELETE n")
        neo4j.close()
    except Exception:
        pass

async def run_locomo():
    print("=" * 75)
    # LoCoMo stands for Long Multi-Session Dialogue Memory
    print("  MemoryOS v1.2.0 - LoCoMo Long Multi-Session Dialogue Benchmark")
    print("=" * 75)
    
    user_id = "locomo_agent"
    workspace_id = "locomo_workspace"
    bg_tasks = BackgroundTasks()
    
    reset_db()
    
    # ------------------------------------------------------------------
    # Step 1: Simulate Multi-Session Conversational Dialogue Ingestion
    # ------------------------------------------------------------------
    sessions_dialogue = {
        "session_001": [
            "Hey! I live in Austin.",
            "I love cooking handmade pasta dish.",
            "I drink black coffee in the mornings."
        ],
        "session_002": [
            "We had a great weekend hiking.",
            "I adopted a golden retriever dog named Max.",
            "Max loves running around in the backyard."
        ],
        "session_003": [
            "I work at Tesla as a robotics engineer.",
            "Austin is a great hub for engineering work.",
            "I uses C++ for writing code."
        ],
        "session_004": [
            "I live in Berlin now, leaving Austin.",
            "I works at BMW now, leaving Tesla.",
            "I speak English and German."
        ],
        "session_005": [
            "I started a side project this week in my free time.",
            "I build a web crawler app in Rust.",
            "I use Rust for its memory safety rules."
        ]
    }
    
    # Ingest sessions sequentially
    print("\n[Phase 1] Simulating dialogue ingestion across 5 sessions...")
    for session_id, turns in sessions_dialogue.items():
        print(f"  Ingesting {session_id}...")
        for turn in turns:
            res = await ingest_memory(
                MemoryIngest(
                    user_id=user_id,
                    content=turn,
                    workspace_id=workspace_id,
                    session_id=session_id
                ),
                bg_tasks
            )
            # Process Neo4j Graph elements for each turn manually to simulate async completion
            background_graph_ingest(res.memory_id, turn, user_id, workspace_id)
            
    # Inject 50 random conversational noise turns from other users to evaluate context leakage
    print(f"\n[Phase 2] Injecting 50 distractor conversations to simulate long-term memory load...")
    distractor_convs = [
        "Did you see the new movie release yesterday?",
        "I need to buy groceries: milk, eggs, bread, and fruits.",
        "The football match ended in a draw last night.",
        "A regular exercise routine keeps your mind sharp.",
        "We are planning a vacation to Hawaii next winter.",
        "The stock market went up today due to inflation numbers.",
        "Can you recommend a good book on design patterns?",
        "Our office is moving to a hybrid work schedule.",
        "I am looking for a recipe for chocolate chip cookies.",
        "The battery life of my smartphone has decreased significantly."
    ]
    for i in range(50):
        noise_text = f"Distractor Turn: {distractor_convs[i % len(distractor_convs)]} #{i}"
        await ingest_memory(
            MemoryIngest(
                user_id="other_user", # Ingest under a different user to act as noise distractors
                content=noise_text,
                workspace_id=workspace_id,
                session_id=f"noise_session_{i // 10}"
            ),
            bg_tasks
        )
    print("  -> Ingested 50 noise turns successfully.")

    # ------------------------------------------------------------------
    # Step 2: Run LoCoMo Retrieval Evaluation Queries
    # ------------------------------------------------------------------
    print("\n[Phase 3] Running LoCoMo evaluation queries...")
    
    locomo_eval_queries = [
        {
            "query": "Where does the agent live currently?",
            "expected": ["Berlin"],
            "forbidden": ["live in Austin"],
            "description": "Cross-session contradiction/update resolution accuracy"
        },
        {
            "query": "What is the name of the dog adopted by the agent?",
            "expected": ["Max"],
            "forbidden": [],
            "description": "Multi-session factual recall precision"
        },
        {
            "query": "Where did the agent work at before BMW?",
            "expected": ["Tesla"],
            "forbidden": ["work at Tesla"],
            "description": "Temporal reasoning & historical placement recall"
        },
        {
            "query": "What Rust project did the agent talk about starting?",
            "expected": ["crawler", "Rust"],
            "forbidden": ["C++", "Tesla"],
            "description": "STM cache recency boost correctness"
        }
    ]
    
    results = []
    
    for eval in locomo_eval_queries:
        latencies = []
        pass_accuracies = []
        contradiction_correct = True
        
        # Run 10 times to measure latency statistics
        for run in range(10):
            t0 = time.time()
            res = await retrieve_context(
                MemoryRetrieve(
                    user_id=user_id,
                    query=eval["query"],
                    limit=3,
                    workspace_id=workspace_id
                )
            )
            latencies.append((time.time() - t0) * 1000)
            
            if run == 0:
                retrieved_text = " ".join([r.content for r in res.results]).lower()
                print(f"\n  Query: '{eval['query']}'")
                print(f"  Description: {eval['description']}")
                print("  Top retrieved items:")
                for r in res.results:
                    print(f"    - [{r.type}] (Score: {r.score:.3f}) {r.content}")
                
                # Check expected matching
                correct = all(exp.lower() in retrieved_text for exp in eval["expected"])
                pass_accuracies.append(correct)
                
                # Check forbidden matching (to verify old values were deactivated)
                if eval["forbidden"]:
                    has_forbidden = any(forb.lower() in retrieved_text for forb in eval["forbidden"])
                    if has_forbidden:
                        contradiction_correct = False
                        
        status = "PASS" if (all(pass_accuracies) and contradiction_correct) else "FAIL"
        results.append({
            "query": eval["query"],
            "status": status,
            "latency": f"{statistics.mean(latencies):.2f}ms",
            "desc": eval["description"]
        })
        
    print("\n" + "=" * 75)
    print("  LoCoMo Benchmark Summary Report")
    print("=" * 75)
    print(f"{'Evaluation Query':<45} | {'Status':<10} | {'Latency':<10}")
    print("-" * 75)
    for r in results:
        print(f"{r['query']:<45} | {r['status']:<10} | {r['latency']:<10}")
    print("=" * 75)

if __name__ == "__main__":
    asyncio.run(run_locomo())
