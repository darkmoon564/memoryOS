import os
import sys
import time
import uuid
import asyncio
import statistics
from fastapi import BackgroundTasks

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.api.memories import (
    ingest_memory, retrieve_context, consolidate_memories, apply_decay
)
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve
from memoryos.services.background import background_graph_ingest
from memoryos.core.cache import stm_cache
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import Neo4jConnector

def reset_database():
    print("[Setup] Initializing database schema...")
    conn = get_postgres_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        try:
            schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../schema.sql"))
            with open(schema_path, "r") as f:
                schema_sql = f.read()
            cur.execute(schema_sql)
        except Exception as e:
            print(f"[Warning] Schema sql execution error: {e}")
            
    print("[Setup] Resetting database tables for benchmark...")
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

async def run_benchmark():
    user_id = "bench_user"
    workspace_id = "default"
    bg_tasks = BackgroundTasks()
    
    print("\n" + "="*70)
    print("  MemoryOS v1.2.0 - Local Benchmark (Modular)")
    print("="*70)
    
    reset_database()
    
    print(f"\n[Phase 1] Injecting distractor noise memories to test precision...")
    noise_templates = [
        "The quick brown fox jumps over the lazy dog.",
        "System update scheduled for midnight on Sunday.",
        "Acme Corp announced record earnings this quarter.",
        "Docker Compose is excellent for local environment orchestration.",
        "The server CPU load spikes every day at 12 PM.",
        "Water boils at 100 degrees Celsius under standard atmospheric pressure.",
        "Kubernetes pods should be configured with resource limits.",
        "The team prefers scheduling meetings in the morning.",
        "We need to implement zero-knowledge encryption for security.",
        "Seattle is known for its coffee culture and rainy weather."
    ]
    
    t_start = time.time()
    for i in range(100):
        content = f"{noise_templates[i % len(noise_templates)]} Random key: {uuid.uuid4().hex[:6]}"
        ingest_data = MemoryIngest(user_id=user_id, content=content, workspace_id=workspace_id)
        await ingest_memory(ingest_data, bg_tasks)
    print(f"  -> Ingested 100 noise memories in {time.time() - t_start:.2f}s")

    print(f"\n[Phase 2] Ingesting temporal preferences and facts...")
    temporal_facts = [
        ("Alice worked at Microsoft in 2022.", "FACTUAL"),
        ("Alice moved to Acme Corp in 2024.", "FACTUAL"),
        ("Alice loves coding backend APIs in Rust.", "PREFERENCE"),
        ("Alice prefers dark mode in all her code editors.", "PREFERENCE")
    ]
    
    for content, expected_type in temporal_facts:
        ingest_data = MemoryIngest(user_id=user_id, content=content, workspace_id=workspace_id)
        res = await ingest_memory(ingest_data, bg_tasks)
        background_graph_ingest(res.memory_id, content, user_id, workspace_id)
    
    print(f"\n[Phase 3] Ingesting contradiction/update scenario...")
    # Fact 1:
    ingest_data_1 = MemoryIngest(user_id=user_id, content="Bob uses VSCode for writing Python scripts.", workspace_id=workspace_id)
    res_1 = await ingest_memory(ingest_data_1, bg_tasks)
    background_graph_ingest(res_1.memory_id, ingest_data_1.content, user_id, workspace_id)
    
    # Fact 2 (Contradiction/Update):
    ingest_data_2 = MemoryIngest(user_id=user_id, content="Bob switched his editor from VSCode to Neovim last night.", workspace_id=workspace_id)
    res_2 = await ingest_memory(ingest_data_2, bg_tasks)
    background_graph_ingest(res_2.memory_id, ingest_data_2.content, user_id, workspace_id)
    
    print("  -> Ingested contradiction facts and executed resolution.")

    print(f"\n[Phase 4] Running evaluations and measuring latency/recall...")
    test_queries = [
        {
            "category": "LongMemEval (Knowledge Update)",
            "query": "Which code editor does Bob use?",
            "expected_contains": ["Neovim"],
            "forbidden_contains": ["uses VSCode", "VSCode for writing"]
        },
        {
            "category": "BEAM (Preference Following)",
            "query": "What are Alice's coding language and layout preferences?",
            "expected_contains": ["Rust", "dark mode"],
            "forbidden_contains": []
        },
        {
            "category": "BEAM (Temporal Reasoning)",
            "query": "Where was Alice working in 2024?",
            "expected_contains": ["Acme Corp"],
            "forbidden_contains": ["Microsoft"]
        },
        {
            "category": "LongMemEval (Abstention)",
            "query": "What is Charlie's favorite programming language?",
            "expected_contains": [],
            "forbidden_contains": ["Rust", "Python", "Neovim"]
        }
    ]
    
    results_summary = []
    
    for test in test_queries:
        latencies = []
        token_counts = []
        pass_accuracy = False
        contradiction_failed = False
        abstention_passed = True
        
        for run in range(10):
            retrieve_data = MemoryRetrieve(user_id=user_id, query=test["query"], limit=3)
            
            t0 = time.time()
            response = await retrieve_context(retrieve_data)
            latencies.append((time.time() - t0) * 1000)
            token_counts.append(response.context_token_count)
            
            if run == 0:
                retrieved_text = " ".join([r.content for r in response.results])
                print(f"\n  [Debug] Query: '{test['query']}'")
                print("  [Debug] Retrieved results:")
                for r in response.results:
                    print(f"    - [{r.type}] (Score: {r.score:.3f}) {r.content}")
                
                if test["expected_contains"]:
                    pass_accuracy = all(exp.lower() in retrieved_text.lower() for exp in test["expected_contains"])
                else:
                    has_high_scores = any(r.score > 0.6 for r in response.results)
                    if has_high_scores:
                        abstention_passed = False
                
                if test["forbidden_contains"]:
                    contradiction_failed = any(forbidden.lower() in retrieved_text.lower() for forbidden in test["forbidden_contains"])
        
        avg_latency = statistics.mean(latencies)
        avg_tokens = statistics.mean(token_counts)
        
        status = "FAIL"
        if test["expected_contains"]:
            if pass_accuracy and not contradiction_failed:
                status = "PASS"
        else:
            if abstention_passed:
                status = "PASS (Abstained)"
                
        results_summary.append({
            "category": test["category"],
            "query": test["query"],
            "status": status,
            "latency": f"{avg_latency:.2f}ms",
            "tokens": int(avg_tokens)
        })
        
    print("\n" + "="*70)
    print("  Local Benchmark Summary Report (Modular)")
    print("="*70)
    print(f"{'Category':<30} | {'Status':<16} | {'Avg Latency':<12} | {'Token Count':<6}")
    print("-"*70)
    for res in results_summary:
        print(f"{res['category']:<30} | {res['status']:<16} | {res['latency']:<12} | {res['tokens']:<6}")
    print("="*70)

if __name__ == "__main__":
    asyncio.run(run_benchmark())
