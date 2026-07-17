import os
import sys
import asyncio
from datetime import datetime, timezone

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.api.memories import retrieve_context
from memoryos.schemas.memory import MemoryRetrieve


class FixedEmbeddingModel:
    """Stable non-zero embedding for exercising pgvector cosine retrieval."""

    vector = [1.0] + [0.0] * 383

    def encode(self, sentences):
        if isinstance(sentences, str):
            return list(self.vector)
        return [list(self.vector) for _ in sentences]


class EqualScoreReranker:
    """Keeps goal boosts, rather than model weights, responsible for ranking."""

    def predict(self, pairs):
        return [1.0] * len(pairs)


def result_for_content(results, content):
    return next(result for result in results if result.content == content)


def test_planner_memory_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Planner Memory & Goal-Aware Tests")
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
    
    user_id = "usr_alice"
    workspace_id = "default"
    
    # Pre-populate user
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
    conn.commit()
    
    # 2. Ingest domain-specific memories
    import uuid
    import json
    
    # Zero vectors are deliberately excluded from pgvector cosine HNSW indexes.
    # Use a deterministic unit vector so this test covers the real retrieval
    # query rather than an index edge case.
    dummy_emb = FixedEmbeddingModel.vector
    emb_str = json.dumps(dummy_emb)
    now = datetime.now(timezone.utc)
    
    print("\nStep 1: Ingesting mixed-domain memories (developer, shopping, research)...")
    with conn.cursor() as cur:
        # Memory A: Developer
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "User prefers coding in Python.", "PREFERENCE", emb_str, 0.70, 1, now, True)
        )
        # Memory B: Shopping
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "User has a budget of 50 USD for tools.", "PREFERENCE", emb_str, 0.65, 1, now, True)
        )
        # Memory C: Research
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "Factual info: Rust is a systems programming language.", "FACTUAL", emb_str, 0.60, 1, now, True)
        )
    conn.commit()
    
    # 3. Test Retrieval with Developer Goal
    print("\nStep 2: Retrieving context with DEVELOPER goal...")
    req_dev = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="What should I use?",
        current_goal="Write a script in Python",
        limit=10
    )
    res_dev = asyncio.run(retrieve_context(req_dev, format="json"))
    
    print(f"  Detected Goal Category: {res_dev.goal_category}")
    assert res_dev.goal_category == "DEVELOPER"
    
    # Verify the developer memory receives a stronger goal boost than an
    # unrelated shopping preference. Do not assert an arbitrary order between
    # multiple developer-related memories.
    results_dev = res_dev.results
    print(f"  Results ordered: {[r.content for r in results_dev]}")
    assert result_for_content(results_dev, "User prefers coding in Python.").score > result_for_content(
        results_dev, "User has a budget of 50 USD for tools."
    ).score
    
    # Test 4: Retrieve with Shopping Goal
    print("\nStep 3: Retrieving context with SHOPPING goal...")
    req_shop = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="What constraints do I have?",
        current_goal="Find tools within budget",
        limit=10
    )
    res_shop = asyncio.run(retrieve_context(req_shop, format="json"))
    
    print(f"  Detected Goal Category: {res_shop.goal_category}")
    assert res_shop.goal_category == "SHOPPING"
    
    results_shop = res_shop.results
    print(f"  Results ordered: {[r.content for r in results_shop]}")
    assert result_for_content(results_shop, "User has a budget of 50 USD for tools.").score > result_for_content(
        results_shop, "User prefers coding in Python."
    ).score
    
    # Test 5: Retrieve with Research Goal
    print("\nStep 4: Retrieving context with RESEARCH goal...")
    req_research = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="Tell me about programming languages.",
        current_goal="Research performance metrics",
        limit=10
    )
    res_research = asyncio.run(retrieve_context(req_research, format="json"))
    
    print(f"  Detected Goal Category: {res_research.goal_category}")
    assert res_research.goal_category == "RESEARCH"
    
    results_research = res_research.results
    print(f"  Results ordered: {[r.content for r in results_research]}")
    # Verify that the factual memory is boosted over a preference.
    assert result_for_content(
        results_research, "Factual info: Rust is a systems programming language."
    ).score > result_for_content(results_research, "User prefers coding in Python.").score
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Planner Memory & Goal-Aware tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    from unittest.mock import patch
    with (
        patch("memoryos.api.memories.verify_workspace_key", return_value=None),
        patch("memoryos.api.memories.get_embedding_model", return_value=FixedEmbeddingModel()),
        patch("memoryos.api.memories.get_reranker_model", return_value=EqualScoreReranker()),
    ):
        test_planner_memory_system()
