import os
import sys

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.core.consolidation import consolidate_hierarchy
from memoryos.api.memories import trigger_consolidation
from memoryos.schemas.memory import MemoryReflect

def test_consolidation_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Memory Consolidation (Hierarchy) Tests")
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
    
    # 2. Add sample episodes representing repeated tasks
    import uuid
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    
    print("\nStep 1: Adding sample episodes with recurring topics (rust, docker)...")
    sample_episodes = [
        "User prefers coding in python and rust.",
        "User configured neovim plugins for rust developer.",
        "User configured docker containers for local dev environment."
    ]
    
    with conn.cursor() as cur:
        for ep in sample_episodes:
            cur.execute(
                """
                INSERT INTO episodes (id, user_id, workspace_id, summary, last_interaction_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), user_id, workspace_id, ep, now, now)
            )
    conn.commit()
    
    # 3. Trigger consolidation hierarchy synthesis
    print("\nStep 2: Triggering consolidate_hierarchy manually...")
    res = consolidate_hierarchy(user_id, workspace_id, neo4j)
    print(f"  Consolidation Response: {res}")
    assert res["status"] == "success"
    
    # 4. Verify graph updates
    print("\nStep 3: Verifying hierarchical entity & relationship creation in Neo4j...")
    if is_mock:
        ents = _mock_graph_data["entities"]
        rels = _mock_graph_data["relationships"]
        print(f"  Mock Entities created: {list(ents.keys())}")
        print(f"  Mock Relationships created: {[(r['source'], r['type'], r['target']) for r in rels]}")
        
        # Verify Topic and Profile entities exist
        topics = [name for name, info in ents.items() if info["type"] == "Topic"]
        profiles = [name for name, info in ents.items() if info["type"] == "Profile"]
        assert len(topics) > 0, "Expected Topic entities to be created"
        assert len(profiles) > 0, "Expected Profile entities to be created"
        
        # Verify relationships
        learning_rels = [r for r in rels if r["type"] == "LEARNING_TOPIC" and r["source"] == user_id]
        has_profile_rels = [r for r in rels if r["type"] == "HAS_PROFILE" and r["source"] == user_id]
        belongs_rels = [r for r in rels if r["type"] == "BELONGS_TO_PROFILE"]
        
        assert len(learning_rels) > 0, "Expected User to connect to Topic via LEARNING_TOPIC"
        assert len(has_profile_rels) > 0, "Expected User to connect to Profile via HAS_PROFILE"
        assert len(belongs_rels) > 0, "Expected Topic to connect to Profile via BELONGS_TO_PROFILE"
    else:
        # Real Neo4j verification
        res_topics = neo4j.query("MATCH (t:Entity {workspace_id: $workspace_id}) WHERE t.type = 'Topic' RETURN t.name AS name", {"workspace_id": workspace_id})
        res_profiles = neo4j.query("MATCH (p:Entity {workspace_id: $workspace_id}) WHERE p.type = 'Profile' RETURN p.name AS name", {"workspace_id": workspace_id})
        assert len(res_topics) > 0, "Expected Topic entities in Neo4j"
        assert len(res_profiles) > 0, "Expected Profile entities in Neo4j"
        
        # Verify relationships
        res_belongs = neo4j.query("MATCH (t:Entity)-[r:BELONGS_TO_PROFILE]->(p:Entity) RETURN t.name AS topic, p.name AS profile")
        assert len(res_belongs) > 0, "Expected Topic to connect to Profile in Neo4j"
        
    # 5. Call API POST /v1/memories/consolidate
    print("\nStep 4: Calling trigger_consolidation API endpoint...")
    req = MemoryReflect(user_id=user_id, workspace_id=workspace_id)
    
    import asyncio
    api_res = asyncio.run(trigger_consolidation(req, authorization="key_default"))
    print(f"  API Response status: {api_res['status']}")
    assert api_res["status"] == "success"
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Memory Consolidation tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_consolidation_system()
