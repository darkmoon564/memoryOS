import os
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.core.temporal_parser import parse_temporal_window
from memoryos.api.memories import retrieve_context
from memoryos.schemas.memory import MemoryRetrieve

def test_temporal_memory_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Temporal Memory & Chronological Tests")
    print("============================================================")
    
    # Current testing date boundary: 2026-07-14 22:45:00 UTC
    current_test_time = datetime(2026, 7, 14, 22, 45, 0, tzinfo=timezone.utc)
    
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
    
    # 2. Ingest memories with historical timestamps
    # Memory A: June 15, 2026 (Alice worked at Acme Corp.)
    # Memory B: July 10, 2026 (Alice configured docker)
    # Memory C: July 14, 2026 (Alice loves neovim) - Today!
    # Memory D: July 13, 2026 (Alice drank green tea) - Yesterday!
    june_date = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    july_mid_date = datetime(2026, 7, 10, 15, 30, 0, tzinfo=timezone.utc)
    yesterday_date = current_test_time - timedelta(days=1)
    today_date = current_test_time - timedelta(minutes=30)
    
    import uuid
    import json
    
    dummy_emb = [0.0] * 384
    emb_str = json.dumps(dummy_emb)
    
    print("\nStep 1: Ingesting memories with custom historical timestamps...")
    with conn.cursor() as cur:
        # Ingest Memory A (June)
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "Alice works at Acme Corp.", "FACTUAL", emb_str, 0.9, 1, june_date, True)
        )
        # Ingest Memory B (July 10)
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "Alice configured Docker containers.", "EPISODIC", emb_str, 0.8, 1, july_mid_date, True)
        )
        # Ingest Memory C (Today)
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "Alice likes neovim plugins.", "PREFERENCE", emb_str, 0.7, 1, today_date, True)
        )
        # Ingest Memory D (Yesterday)
        cur.execute(
            """
            INSERT INTO memories (id, user_id, workspace_id, content, memory_type, embedding, importance_score, frequency_count, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (str(uuid.uuid4()), user_id, workspace_id, "Alice drank green tea with ginger.", "EPISODIC", emb_str, 0.5, 1, yesterday_date, True)
        )
    conn.commit()
    
    # 3. Test Temporal Parsing helper
    print("\nStep 2: Testing temporal window parsing rules...")
    
    # Yesterday parsing
    ys, ye = parse_temporal_window("what did i do yesterday?", current_time=current_test_time)
    print(f"  Query 'yesterday': start={ys}, end={ye}")
    assert ys is not None and ye is not None
    assert ys.day == 13 and ye.day == 13
    
    # June parsing
    js, je = parse_temporal_window("Where was I working in June?", current_time=current_test_time)
    print(f"  Query 'June': start={js}, end={je}")
    assert js is not None and je is not None
    assert js.month == 6 and je.month == 6
    assert js.year == 2026 and je.year == 2026
    
    # 4. Test RAG Context Retrieval with Time Constraints
    print("\nStep 3: Triggering retrieve_context API with temporal queries...")
    
    # Test A: June Query
    req_june = MemoryRetrieve(user_id=user_id, workspace_id=workspace_id, query="Where was I working in June?", limit=10)
    
    # Keep relative windows deterministic while exercising the production
    # retrieval path with the same clock used to seed the records.
    with patch(
        "memoryos.api.memories.parse_temporal_window",
        lambda query: parse_temporal_window(query, current_time=current_test_time),
    ):
        res_june = asyncio.run(retrieve_context(req_june, format="markdown"))
    markdown_text = res_june["markdown"]
    print("\n  Markdown Context for 'June' query:")
    print(markdown_text)
    
    # Assert Memory A (Acme Corp) is present, and Memory B/C/D are absent
    assert "Acme Corp." in markdown_text, "Expected Memory A to be retrieved for June query"
    assert "Docker" not in markdown_text, "Did not expect July memory in June query"
    assert "neovim" not in markdown_text, "Did not expect today's memory in June query"
    assert "## Chronological Timeline" in markdown_text, "Expected a chronological timeline section"
    assert "[2026-06-15]" in markdown_text, "Expected date format in chronological timeline"
    
    # Test B: Yesterday Query
    req_yest = MemoryRetrieve(user_id=user_id, workspace_id=workspace_id, query="what did i do yesterday?", limit=10)
    with patch(
        "memoryos.api.memories.parse_temporal_window",
        lambda query: parse_temporal_window(query, current_time=current_test_time),
    ):
        res_yest = asyncio.run(retrieve_context(req_yest, format="markdown"))
    markdown_yest = res_yest["markdown"]
    print("\n  Markdown Context for 'yesterday' query:")
    print(markdown_yest)
    
    # Assert Memory D (green tea) is present
    assert "green tea" in markdown_yest, "Expected yesterday's memory to be retrieved"
    assert "Acme Corp." not in markdown_yest, "Did not expect June memory in yesterday's query"
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Temporal Memory & Chronological tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    from unittest.mock import patch
    with patch("memoryos.api.memories.verify_workspace_key", return_value=None):
        test_temporal_memory_system()
