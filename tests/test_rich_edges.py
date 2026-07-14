import os
import sys
import time

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.background import background_graph_ingest
from memoryos.config import _mock_graph_data

def test_rich_edges():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Rich Edge Metadata & versioning Tests")
    print("============================================================")
    
    # 1. Reset Database State
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
    
    # 2. Ingest Fact from Memory 1 (First assertion)
    print("\nStep 1: Ingesting 'Alice works at Acme.' from source memory 'm1'...")
    background_graph_ingest("m1", "Alice works at Acme.", user_id, workspace_id)
    
    # Assertions for Ingestion 1
    if is_mock:
        rels = _mock_graph_data["relationships"]
        assert len(rels) == 1, f"Expected 1 relationship, got {len(rels)}"
        r = rels[0]
        print(f"  Assertion 1 (Create): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 1, "Expected version 1"
        assert r.get("evidence_count") == 1, "Expected evidence_count 1"
        assert r.get("source_memory_id") == "m1", "Expected source_memory_id 'm1'"
        assert r.get("confidence") == 0.9, "Expected default confidence 0.9"
        assert r.get("workspace_id") == "default", "Expected workspace_id 'default'"
        assert "timestamp" in r, "Expected timestamp property to exist"
    else:
        res = neo4j.query("MATCH (:Entity)-[r:WORKS_AT]->(:Entity) RETURN r")
        assert len(res) == 1, "Expected 1 relationship in Neo4j"
        r = res[0]["r"]
        print(f"  Assertion 1 (Create): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 1
        assert r.get("evidence_count") == 1
        assert r.get("source_memory_id") == "m1"
        
    # 3. Ingest SAME Fact from SAME Memory 1 (Duplicate Assertion)
    print("\nStep 2: Ingesting 'Alice works at Acme.' again from SAME source memory 'm1'...")
    background_graph_ingest("m1", "Alice works at Acme.", user_id, workspace_id)
    
    # Assertions for Ingestion 2
    if is_mock:
        rels = _mock_graph_data["relationships"]
        assert len(rels) == 1, "Should still be a single relationship"
        r = rels[0]
        print(f"  Assertion 2 (Match same source): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 2, f"Expected version 2, got {r.get('version')}"
        assert r.get("evidence_count") == 1, f"Expected evidence_count 1 (no change), got {r.get('evidence_count')}"
        assert r.get("source_memory_id") == "m1", "Expected source_memory_id 'm1'"
    else:
        res = neo4j.query("MATCH (:Entity)-[r:WORKS_AT]->(:Entity) RETURN r")
        r = res[0]["r"]
        print(f"  Assertion 2 (Match same source): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 2
        assert r.get("evidence_count") == 1
        assert r.get("source_memory_id") == "m1"
        
    # 4. Ingest SAME Fact from DIFFERENT Memory 2 (New Evidence)
    print("\nStep 3: Ingesting 'Alice works at Acme.' from DIFFERENT source memory 'm2'...")
    background_graph_ingest("m2", "Alice works at Acme.", user_id, workspace_id)
    
    # Assertions for Ingestion 3
    if is_mock:
        rels = _mock_graph_data["relationships"]
        assert len(rels) == 1, "Should still be a single relationship"
        r = rels[0]
        print(f"  Assertion 3 (Match new source): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 3, f"Expected version 3, got {r.get('version')}"
        assert r.get("evidence_count") == 2, f"Expected evidence_count 2 (incremented), got {r.get('evidence_count')}"
        assert r.get("source_memory_id") == "m2", "Expected source_memory_id updated to 'm2'"
    else:
        res = neo4j.query("MATCH (:Entity)-[r:WORKS_AT]->(:Entity) RETURN r")
        r = res[0]["r"]
        print(f"  Assertion 3 (Match new source): version={r.get('version')}, evidence_count={r.get('evidence_count')}, source_memory_id={r.get('source_memory_id')}")
        assert r.get("version") == 3
        assert r.get("evidence_count") == 2
        assert r.get("source_memory_id") == "m2"
        
    print("\n" + "=" * 60)
    print("  Rich Edge Metadata & versioning tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_rich_edges()
