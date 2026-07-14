import os
import sys

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.core.reflection import run_reflection
from memoryos.api.memories import trigger_reflection
from memoryos.schemas.memory import MemoryReflect

def test_reflection_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Reflection System & Daemon Tests")
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
    workspace_id = "default"
    
    # Pre-populate user and session to satisfy foreign key constraints
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
    conn.commit()
    
    # 2. Add sample episodes representing repeated patterns
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    print("\nStep 1: Adding sample episodes with recurring topics (neovim, python, docker)...")
    sample_episodes = [
        "User prefers coding in python.",
        "User configured neovim plugins.",
        "User configured docker containers for local dev environment."
    ]
    
    with conn.cursor() as cur:
        for ep in sample_episodes:
            ep_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO episodes (id, user_id, workspace_id, summary, last_interaction_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (ep_id, user_id, workspace_id, ep, now, now)
            )
            cur.execute(
                """
                INSERT INTO conversation_logs (id, user_id, workspace_id, episode_id, content, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), user_id, workspace_id, ep_id, f"Raw interaction: {ep}", now)
            )
    conn.commit()
    
    # 3. Trigger reflection synthesis
    print("\nStep 2: Triggering run_reflection manually...")
    facts = run_reflection(user_id, workspace_id, neo4j)
    
    # Verify raw logs were archived and deleted
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM conversation_logs WHERE user_id = %s", (user_id,))
        cnt = cur.fetchone()[0]
        assert cnt == 0, f"Expected 0 ephemeral conversation logs remaining in DB, found {cnt}"
    
    # 4. Verify graph updates
    print("\nStep 3: Verifying relationship creation in Neo4j...")
    if is_mock:
        rels = _mock_graph_data["relationships"]
        print(f"  Mock Relationships created: {[(r['source'], r['type'], r['target']) for r in rels]}")
        
        # Verify synthesized relationship (e.g. user USES python or docker or neovim)
        uses_rels = [r for r in rels if r["type"] in ["USES", "WORKS_AT", "INTERESTED_IN", "PREFERS"] and r["source"] == "user"]
        assert len(uses_rels) > 0, "Expected at least 1 synthesized relationship from 'user'"
        
        # Verify User node connections
        knows_rels = [r for r in rels if r["type"] == "KNOWS_ABOUT" and r["source"] == user_id]
        assert len(knows_rels) > 0, "Expected User node to connect to entities via KNOWS_ABOUT"
    else:
        # Real Neo4j verification
        res_rels = neo4j.query(
            "MATCH (s:Entity {workspace_id: $workspace_id})-[r]->(t:Entity) "
            "WHERE s.name = 'user' RETURN type(r) AS type, t.name AS target",
            {"workspace_id": workspace_id}
        )
        print(f"  Neo4j Relationships created: {[(record['type'], record['target']) for record in res_rels]}")
        assert len(res_rels) > 0, "Expected synthesized relationships in Neo4j"
        
        res_knows = neo4j.query(
            "MATCH (u:User {id: $user_id})-[r:KNOWS_ABOUT]->(e:Entity) RETURN e.name AS target",
            {"user_id": user_id}
        )
        print(f"  User KNOWS_ABOUT entities: {[record['target'] for record in res_knows]}")
        assert len(res_knows) > 0, "Expected User to connect via KNOWS_ABOUT in Neo4j"
        
    # 5. Call API POST /v1/memories/reflect
    print("\nStep 4: Calling trigger_reflection API endpoint...")
    req = MemoryReflect(user_id=user_id, workspace_id=workspace_id)
    
    import asyncio
    api_res = asyncio.run(trigger_reflection(req))
    print(f"  API Response status: {api_res['status']}")
    print(f"  API Response synthesized facts: {api_res['facts']}")
    assert api_res["status"] == "success"
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Reflection System & Daemon tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_reflection_system()
