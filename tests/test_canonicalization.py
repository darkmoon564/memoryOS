import os
import sys
import time

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.neo4j import get_neo4j_conn, MockNeo4jDriver
from memoryos.services.background import background_graph_ingest
from memoryos.config import _mock_graph_data

def test_canonicalization():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Entity Canonicalization Tests")
    print("============================================================")
    
    # 1. Reset Mock Graph Data if using Mock driver
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
        
    user_id = "usr_bob"
    workspace_id = "test_ws"
    
    # 2. Ingest compound facts about Microsoft with variations
    print("\nStep 1: Ingesting 'Bob works at Microsoft Corp.'")
    background_graph_ingest("m1", "Bob works at Microsoft Corp.", user_id, workspace_id)
    
    print("\nStep 2: Ingesting 'Bob prefers Microsoft.'")
    background_graph_ingest("m2", "Bob prefers Microsoft.", user_id, workspace_id)
    
    print("\nStep 3: Ingesting 'Bob uses MSFT.'")
    background_graph_ingest("m3", "Bob uses MSFT.", user_id, workspace_id)
    
    time.sleep(0.2)
    
    # 3. Assertions
    print("\nStep 4: Verifying Canonicalization Results...")
    
    if is_mock:
        # Check Entities
        entities = _mock_graph_data["entities"]
        aliases = _mock_graph_data["aliases"]
        relationships = _mock_graph_data["relationships"]
        
        print(f"  Existing Entities: {list(entities.keys())}")
        print(f"  Existing Aliases:  {aliases}")
        
        # Either 'microsoft' or 'microsoft corp.' will be canonical, and the other is an alias
        canonical_name = "microsoft corp." if "microsoft corp." in entities else "microsoft"
        alias_name = "microsoft" if canonical_name == "microsoft corp." else "microsoft corp."
        
        assert canonical_name in entities, f"Expected '{canonical_name}' to be a canonical entity node"
        assert aliases.get(alias_name) == canonical_name, f"Expected alias '{alias_name}' -> '{canonical_name}'"
        
        # Relationships should be on the canonical entity
        works_at_rel = [r for r in relationships if r["type"] == "WORKS_AT"]
        assert len(works_at_rel) > 0, "Expected a WORKS_AT relationship"
        assert works_at_rel[0]["target"] == canonical_name, f"Expected WORKS_AT target to be canonical '{canonical_name}', got '{works_at_rel[0]['target']}'"
        
        uses_rel = [r for r in relationships if r["type"] == "USES"]
        assert len(uses_rel) > 0, "Expected a USES relationship"
        # Since 'msft' is distinct, it links to its own canonical node 'msft'
        assert uses_rel[0]["target"] == "msft", f"Expected USES target to be 'msft', got '{uses_rel[0]['target']}'"
        
        print("  [PASS] Mock assertions verified successfully.")
    else:
        # Verify via Cypher queries
        ent_count = neo4j.query("MATCH (e:Entity {workspace_id: $ws}) RETURN count(e) AS count", {"ws": workspace_id})
        print(f"  Canonical Entity Count: {ent_count[0]['count']}")
        assert ent_count[0]['count'] == 3, f"Expected exactly 3 canonical entities (bob, microsoft corp., msft), got {ent_count[0]['count']}"
        
        alias_rel_count = neo4j.query(
            "MATCH (a:Alias {workspace_id: $ws})-[:ALIAS_OF]->(e:Entity {name: 'microsoft corp.'}) RETURN count(a) AS count",
            {"ws": workspace_id}
        )
        print(f"  Alias Relationships Count: {alias_rel_count[0]['count']}")
        assert alias_rel_count[0]['count'] == 1, f"Expected 1 alias pointing to 'microsoft corp.', got {alias_rel_count[0]['count']}"
        
        print("  [PASS] Neo4j Cypher assertions verified successfully.")
        
    print("\n" + "=" * 60)
    print("  Entity Canonicalization tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_canonicalization()
