import os
import sys

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.background import background_graph_ingest
from memoryos.config import _mock_graph_data

def test_contradiction_engine():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Contradiction Engine Tests")
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
    
    # 2. Ingest initial fact: "Alice lives in Austin."
    print("\nStep 1: Ingesting 'Alice lives in Austin.'...")
    background_graph_ingest("m1", "Alice lives in Austin.", user_id, workspace_id)
    
    # Verify initial state
    if is_mock:
        rels = _mock_graph_data["relationships"]
        lives_in_rels = [r for r in rels if r["type"] == "LIVES_IN"]
        assert len(lives_in_rels) == 1, f"Expected 1 LIVES_IN relationship, got {len(lives_in_rels)}"
        r = lives_in_rels[0]
        print(f"  Initial relation: source={r['source']}, target={r['target']}, is_active={r['is_active']}, valid_from={r.get('valid_from')}, valid_to={r.get('valid_to')}")
        assert r["target"] == "austin", "Expected target 'austin'"
        assert r["is_active"] is True, "Expected relationship to be active"
        assert r.get("valid_from") is not None, "Expected valid_from to be set"
        assert r.get("valid_to") is None, "Expected valid_to to be None"
    else:
        res = neo4j.query(
            "MATCH (:Entity)-[r:LIVES_IN]->(:Entity) "
            "RETURN r.is_active AS is_active, r.valid_from AS valid_from, r.valid_to AS valid_to"
        )
        assert len(res) == 1, "Expected 1 LIVES_IN relationship in Neo4j"
        row = res[0]
        print(f"  Initial relation: is_active={row['is_active']}, valid_from={row['valid_from']}, valid_to={row['valid_to']}")
        assert row["is_active"] is True
        assert row["valid_from"] is not None
        assert row["valid_to"] is None
        
    # 3. Ingest contradicting fact: "Alice lives in Berlin."
    print("\nStep 2: Ingesting contradicting fact 'Alice lives in Berlin.'...")
    background_graph_ingest("m2", "Alice lives in Berlin.", user_id, workspace_id)
    
    # Verify contradiction resolution and versioned deactivation
    if is_mock:
        rels = _mock_graph_data["relationships"]
        
        # Verify LIVES_IN relationships
        lives_in_rels = [r for r in rels if r["type"] == "LIVES_IN"]
        assert len(lives_in_rels) == 2, f"Expected 2 LIVES_IN relationships, got {len(lives_in_rels)}"
        
        r_old = next(r for r in lives_in_rels if r["target"] == "austin")
        r_new = next(r for r in lives_in_rels if r["target"] == "berlin")
        
        print("\n  Verifying Old Fact (Austin):")
        print(f"    is_active={r_old['is_active']}, valid_from={r_old.get('valid_from')}, valid_to={r_old.get('valid_to')}, superseded_by={r_old.get('superseded_by')}")
        assert r_old["is_active"] is False, "Old fact should be deactivated"
        assert r_old.get("valid_to") is not None, "Old fact should have valid_to set"
        assert r_old.get("superseded_by") == "berlin", "Old fact should point to 'berlin' as superseded_by"
        
        print("\n  Verifying New Fact (Berlin):")
        print(f"    is_active={r_new['is_active']}, valid_from={r_new.get('valid_from')}, valid_to={r_new.get('valid_to')}")
        assert r_new["is_active"] is True, "New fact should be active"
        assert r_new.get("valid_from") is not None, "New fact should have valid_from set"
        assert r_new.get("valid_to") is None, "New fact should have valid_to as None"
        
        # Verify SUPERSEDED_BY relationship
        ss_rels = [r for r in rels if r["type"] == "SUPERSEDED_BY"]
        assert len(ss_rels) == 1, f"Expected 1 SUPERSEDED_BY relationship, got {len(ss_rels)}"
        ss = ss_rels[0]
        print("\n  Verifying SUPERSEDED_BY edge:")
        print(f"    source={ss['source']}, target={ss['target']}, relationship_type={ss.get('relationship_type')}, subject={ss.get('subject')}, timestamp={ss.get('timestamp')}")
        assert ss["source"] == "austin", "Expected source to be 'austin'"
        assert ss["target"] == "berlin", "Expected target to be 'berlin'"
        assert ss.get("relationship_type") == "LIVES_IN", "Expected type 'LIVES_IN'"
        assert ss.get("subject") == "alice", "Expected subject 'alice'"
        assert ss.get("timestamp") is not None, "Expected timestamp to be set"
    else:
        # Real Neo4j verification
        # Fetch old deactivated relationship
        res_old = neo4j.query(
            "MATCH (s:Entity)-[r:LIVES_IN]->(t:Entity {name: 'austin'}) "
            "RETURN r.is_active AS is_active, r.valid_from AS valid_from, "
            "r.valid_to AS valid_to, r.superseded_by AS superseded_by"
        )
        r_old = res_old[0]
        print("\n  Verifying Old Fact (Austin):")
        print(f"    is_active={r_old['is_active']}, valid_from={r_old['valid_from']}, valid_to={r_old['valid_to']}, superseded_by={r_old['superseded_by']}")
        assert r_old["is_active"] is False
        assert r_old["valid_to"] is not None
        assert r_old["superseded_by"] == "berlin"
        
        # Fetch new active relationship
        res_new = neo4j.query(
            "MATCH (s:Entity)-[r:LIVES_IN]->(t:Entity {name: 'berlin'}) "
            "RETURN r.is_active AS is_active, r.valid_from AS valid_from, r.valid_to AS valid_to"
        )
        r_new = res_new[0]
        print("\n  Verifying New Fact (Berlin):")
        print(f"    is_active={r_new['is_active']}, valid_from={r_new['valid_from']}, valid_to={r_new['valid_to']}")
        assert r_new["is_active"] is True
        assert r_new["valid_from"] is not None
        assert r_new["valid_to"] is None
        
        # Fetch SUPERSEDED_BY edge
        res_ss = neo4j.query(
            "MATCH (old)-[r:SUPERSEDED_BY]->(new) "
            "RETURN old.name AS source, new.name AS target, "
            "r.relationship_type AS relationship_type, r.subject AS subject, r.timestamp AS timestamp"
        )
        assert len(res_ss) == 1
        ss_row = res_ss[0]
        print("\n  Verifying SUPERSEDED_BY edge:")
        print(f"    source={ss_row['source']}, target={ss_row['target']}, relationship_type={ss_row['relationship_type']}, subject={ss_row['subject']}, timestamp={ss_row['timestamp']}")
        assert ss_row["source"] == "austin"
        assert ss_row["target"] == "berlin"
        assert ss_row["relationship_type"] == "LIVES_IN"
        assert ss_row["subject"] == "alice"
        assert ss_row["timestamp"] is not None
        
    print("\n" + "=" * 60)
    print("  Contradiction Engine tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_contradiction_engine()
