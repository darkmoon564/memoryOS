import os
import sys
from datetime import datetime, timezone, timedelta

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.models.embeddings import get_embedding_model
from memoryos.core.episodes import process_conversation_log

def test_vector_memory():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Vector Memory (Episodes) Tests")
    print("============================================================")
    
    # 1. Reset Database State
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        # Clear tables in order
        cur.execute("DELETE FROM conversation_logs")
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM episodes")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM users")
    conn.commit()
    
    # Enable fallback schema check
    is_sqlite = "sqlite" in str(type(conn)).lower()
    if is_sqlite:
        print("[INFO] Operating in SQLite fallback database mode.")
    else:
        print("[INFO] Operating in Real PostgreSQL database mode.")
        
    user_id = "usr_alice"
    session_id = "sess_1"
    workspace_id = "default"
    
    # Pre-populate user and session to satisfy foreign keys
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        cur.execute("INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING", (session_id, user_id))
    conn.commit()
    
    embedding_model = get_embedding_model()
    
    # 2. Ingest first raw log: "Hello, I need help coding."
    print("\nStep 1: Ingesting first conversation log: 'Hello, I need help coding.'...")
    ep_id_1 = process_conversation_log(conn, user_id, session_id, workspace_id, "Hello, I need help coding.", embedding_model)
    conn.commit()
    
    # Verify Step 1
    with conn.cursor() as cur:
        # Get logs
        cur.execute("SELECT id, episode_id, content FROM conversation_logs")
        logs = cur.fetchall()
        assert len(logs) == 1, f"Expected 1 conversation log, got {len(logs)}"
        print(f"  Logs: {[(l[0] if not hasattr(l, 'keys') else l['id'], l[2] if not hasattr(l, 'keys') else l['content']) for l in logs]}")
        
        # Get episodes
        cur.execute("SELECT id, summary, embedding FROM episodes")
        episodes = cur.fetchall()
        assert len(episodes) == 1, f"Expected 1 episode, got {len(episodes)}"
        ep = episodes[0]
        summary = ep[1] if not hasattr(ep, 'keys') else ep['summary']
        embedding = ep[2] if not hasattr(ep, 'keys') else ep['embedding']
        
        print(f"  Episode Summary: '{summary}'")
        assert summary is not None, "Summary should not be None"
        
        # Handle sqlite/postgres raw vs string vs list deserialization for vector
        if isinstance(embedding, str):
            import json
            embedding = json.loads(embedding)
        assert len(embedding) == 384, f"Expected 384 dimensions, got {len(embedding)}"
        
    # 3. Ingest second raw log shortly after: "Also, I prefer using Python."
    print("\nStep 2: Ingesting second log: 'Also, I prefer using Python.' (should append to same episode)...")
    ep_id_2 = process_conversation_log(conn, user_id, session_id, workspace_id, "Also, I prefer using Python.", embedding_model)
    conn.commit()
    
    # Verify Step 2
    assert ep_id_1 == ep_id_2, f"Expected same episode ID, got {ep_id_1} vs {ep_id_2}"
    with conn.cursor() as cur:
        # Get logs
        cur.execute("SELECT id, episode_id, content FROM conversation_logs")
        logs = cur.fetchall()
        assert len(logs) == 2, f"Expected 2 conversation logs, got {len(logs)}"
        
        # Get episodes
        cur.execute("SELECT id, summary FROM episodes")
        episodes = cur.fetchall()
        assert len(episodes) == 1, f"Expected still 1 episode, got {len(episodes)}"
        print(f"  Updated Episode Summary: '{episodes[0][1] if not hasattr(episodes[0], 'keys') else episodes[0]['summary']}'")
        
    # 4. Simulate time gap (backdate the last_interaction_at of episode 1 to 1 hour ago)
    print("\nStep 3: Backdating episode to simulate a 1-hour time gap...")
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE episodes SET last_interaction_at = %s WHERE id = %s", (one_hour_ago, ep_id_1))
    conn.commit()
    
    # Ingest third raw log: "Actually, let's switch to Rust."
    print("Ingesting third log: 'Actually, let's switch to Rust.' (should create a NEW episode)...")
    ep_id_3 = process_conversation_log(conn, user_id, session_id, workspace_id, "Actually, let's switch to Rust.", embedding_model)
    conn.commit()
    
    # Verify Step 4
    assert ep_id_3 != ep_id_1, f"Expected new episode ID, got same {ep_id_3}"
    with conn.cursor() as cur:
        # Get episodes
        cur.execute("SELECT id, summary FROM episodes ORDER BY created_at ASC")
        episodes = cur.fetchall()
        assert len(episodes) == 2, f"Expected 2 episodes, got {len(episodes)}"
        
        print("\n  Verifying Episode 1:")
        ep1 = episodes[0]
        sum1 = ep1[1] if not hasattr(ep1, 'keys') else ep1['summary']
        print(f"    Summary: '{sum1}'")
        
        print("\n  Verifying Episode 2:")
        ep2 = episodes[1]
        sum2 = ep2[1] if not hasattr(ep2, 'keys') else ep2['summary']
        print(f"    Summary: '{sum2}'")
        
    conn.close()
    print("\n" + "=" * 60)
    print("  Vector Memory (Episodes) tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_vector_memory()
