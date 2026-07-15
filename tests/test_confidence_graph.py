import os
os.environ["TESTING"] = "1"
import sys
import asyncio
from datetime import datetime, timezone

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.core.graph_expander import expand_entities
from memoryos.api.memories import ingest_memory, retrieve_context
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve

def test_confidence_graph_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Confidence Engine & Graph Upgrades Tests")
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
        print("[INFO] Resetting mock graph database...")
        neo4j._driver.data["entities"].clear()
        neo4j._driver.data["relationships"].clear()
    else:
        neo4j.query("MATCH (n) DETACH DELETE n")
        
    user_id = "usr_alice"
    workspace_id = "default"
    
    # Pre-populate user
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
    conn.commit()
    
    # 2. Test Multidimensional Confidence Engine
    print("\nStep 1: Ingesting factual clause...")
    req_ing = MemoryIngest(
        user_id=user_id,
        workspace_id=workspace_id,
        content="Alice lives in Seattle."
    )
    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    asyncio.run(ingest_memory(req_ing, bg))
    
    print("Step 2: Retrieving context to verify multidimensional scoring...")
    req_ret = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="Where does Alice live?",
        limit=5
    )
    res_ret = asyncio.run(retrieve_context(req_ret, format="json"))
    
    assert len(res_ret.results) > 0
    item = res_ret.results[0]
    print(f"  Memory Item: {item.content}")
    print(f"  Confidence: {item.confidence}")
    print(f"  Importance: {item.importance}")
    print(f"  Frequency: {item.frequency}")
    print(f"  Recency: {item.recency}")
    print(f"  Verification: {item.verification}")
    print(f"  Source: {item.source}")
    print(f"  Decay: {item.decay}")
    
    # Assert multidimensional attributes are populated with logical defaults
    assert item.confidence > 0.0
    assert 0.40 <= item.importance <= 0.80
    assert item.frequency == 1
    assert item.recency > 0.99
    assert item.verification == "verified"
    assert item.source == "user"
    assert item.decay == 1.0
    
    # 3. Test Graph Traversal Upgrades
    print("\nStep 3: Seeding multi-hop and community topics in Neo4j...")
    if is_mock:
        # Seed Mock relationships
        # Alice -> LEARNING_TOPIC -> Vellum
        # Vellum -> BELONGS_TO_TOPIC -> Rust
        # Cargo -> BELONGS_TO_TOPIC -> Rust
        neo4j._driver.data["entities"]["Alice"] = {"type": "Entity", "workspace": workspace_id}
        neo4j._driver.data["entities"]["Vellum"] = {"type": "Entity", "workspace": workspace_id}
        neo4j._driver.data["entities"]["Rust"] = {"type": "Entity", "workspace": workspace_id}
        neo4j._driver.data["entities"]["Cargo"] = {"type": "Entity", "workspace": workspace_id}
        
        neo4j._driver.data["relationships"].extend([
            {"source": "Alice", "target": "Vellum", "type": "LEARNING_TOPIC", "workspace_id": workspace_id, "is_active": True},
            {"source": "Vellum", "target": "Rust", "type": "BELONGS_TO_TOPIC", "workspace_id": workspace_id, "is_active": True},
            {"source": "Cargo", "target": "Rust", "type": "BELONGS_TO_TOPIC", "workspace_id": workspace_id, "is_active": True}
        ])
    else:
        neo4j.query("CREATE (:Entity {name: 'Alice', workspace_id: $ws})", {"ws": workspace_id})
        neo4j.query("CREATE (:Entity {name: 'Vellum', workspace_id: $ws})", {"ws": workspace_id})
        neo4j.query("CREATE (:Entity {name: 'Rust', workspace_id: $ws})", {"ws": workspace_id})
        neo4j.query("CREATE (:Entity {name: 'Cargo', workspace_id: $ws})", {"ws": workspace_id})
        
        neo4j.query("MATCH (a:Entity {name: 'Alice'}), (b:Entity {name: 'Vellum'}) CREATE (a)-[:LEARNING_TOPIC {is_active: true, workspace_id: $ws}]->(b)", {"ws": workspace_id})
        neo4j.query("MATCH (a:Entity {name: 'Vellum'}), (b:Entity {name: 'Rust'}) CREATE (a)-[:BELONGS_TO_TOPIC {is_active: true, workspace_id: $ws}]->(b)", {"ws": workspace_id})
        neo4j.query("MATCH (a:Entity {name: 'Cargo'}), (b:Entity {name: 'Rust'}) CREATE (a)-[:BELONGS_TO_TOPIC {is_active: true, workspace_id: $ws}]->(b)", {"ws": workspace_id})

    print("Step 4: Executing variable length path traversal (1..3 hops)...")
    res_multihop = expand_entities(neo4j, user_id, workspace_id, ["Alice"])
    print(f"  Expanded entities: {res_multihop['expanded_entities']}")
    print(f"  Graph Facts: {res_multihop['graph_facts']}")
    
    # Assert that multi-hop reaches Entity C "Rust"
    assert "rust" in res_multihop["expanded_entities"]
    assert any("vellum belongs to topic rust" in f.lower() for f in res_multihop["graph_facts"])

    print("Step 5: Executing semantic community neighbor expansion...")
    res_neighbor = expand_entities(neo4j, user_id, workspace_id, ["Cargo"])
    print(f"  Neighbor Graph Facts: {res_neighbor['graph_facts']}")
    
    # Assert that Cargo matches its topic neighbor Vellum (sharing parent topic Rust)
    assert any("vellum belongs to same cluster topic 'rust' as cargo" in f.lower() for f in res_neighbor["graph_facts"])

    print("Step 6: Executing shortest path query between concept pairs...")
    res_shortest = expand_entities(neo4j, user_id, workspace_id, ["Alice", "Rust"])
    print(f"  Shortest Path Facts: {res_shortest['graph_facts']}")
    
    # Assert that path facts linking Alice -> Vellum -> Rust are returned
    assert any("alice learning topic vellum" in f.lower() for f in res_shortest["graph_facts"])
    assert any("vellum belongs to topic rust" in f.lower() for f in res_shortest["graph_facts"])

    conn.close()
    print("\n" + "=" * 60)
    print("  Confidence Engine & Graph Upgrades tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    from unittest.mock import patch
    with patch("memoryos.api.memories.verify_workspace_key", return_value=None):
        test_confidence_graph_system()
