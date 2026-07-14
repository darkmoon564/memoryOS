import os
import sys
import asyncio
from datetime import datetime, timezone

# Ensure package is in path if run directly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data
from memoryos.api.memories import ingest_workflow, retrieve_context
from memoryos.schemas.memory import WorkflowIngest, MemoryRetrieve

def test_procedural_memory_system():
    print("============================================================")
    print("  MemoryOS v1.2.0 - Procedural Memory (Workflows) Tests")
    print("============================================================")
    
    # 1. Reset Database State
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM conversation_logs")
        cur.execute("DELETE FROM memories")
        cur.execute("DELETE FROM episodes")
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM workflows")
        cur.execute("DELETE FROM users")
    conn.commit()
    
    neo4j = get_neo4j_conn()
    is_mock = getattr(neo4j, "is_mock", False)
    
    if is_mock:
        print("[INFO] Operating in Neo4j Mock Mode. Resetting mock data...")
        _mock_graph_data["entities"].clear()
        _mock_graph_data["relationships"].clear()
    else:
        print("[INFO] Operating in Real Neo4j Mode. Purging database...")
        neo4j.query("MATCH (n) DETACH DELETE n")
        
    user_id = "usr_alice"
    workspace_id = "default"
    
    # Pre-populate user
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
    conn.commit()
    
    # 2. Ingest structured workflow
    print("\nStep 1: Ingesting structured workflow via API...")
    req_wf = WorkflowIngest(
        user_id=user_id,
        workspace_id=workspace_id,
        name="deploy application",
        description="How the user deploys: Docker -> Railway -> Postgres",
        steps=[
            "Build docker image locally",
            "Push to Railway registry",
            "Sync Postgres migration scripts"
        ]
    )
    
    res_wf = asyncio.run(ingest_workflow(req_wf))
    print(f"  Ingestion response: {res_wf.status} - {res_wf.message}")
    assert res_wf.status == "success"
    
    # 3. Verify Database storage
    print("\nStep 2: Verifying workflow storage in PostgreSQL/SQLite...")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM workflows WHERE user_id = %s", (user_id,))
        cnt = cur.fetchone()[0]
        assert cnt == 1, f"Expected 1 workflow in DB, found {cnt}"
        
        cur.execute("SELECT name, description, steps FROM workflows WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
        import json
        steps = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        print(f"  Stored workflow steps: {steps}")
        assert row[0] == "deploy application"
        assert len(steps) == 3
        
    # 4. Verify Neo4j updates
    print("\nStep 3: Verifying workflow nodes & relationships in Neo4j...")
    if is_mock:
        ents = _mock_graph_data["entities"]
        rels = _mock_graph_data["relationships"]
        print(f"  Mock Entities created: {list(ents.keys())}")
        print(f"  Mock Relationships created: {[(r['source'], r['type'], r['target']) for r in rels]}")
        
        assert "deploy application" in ents
        assert ents["deploy application"]["type"] == "Workflow"
        
        has_wf_rels = [r for r in rels if r["type"] == "HAS_WORKFLOW" and r["source"] == user_id]
        uses_tech_rels = [r for r in rels if r["type"] == "USES_TECH" and r["source"] == "deploy application"]
        
        assert len(has_wf_rels) > 0, "Expected User to link to Workflow"
        assert len(uses_tech_rels) > 0, "Expected Workflow to link to detected Tech (e.g. docker or postgres)"
    else:
        res_wf_nodes = neo4j.query("MATCH (w:Workflow {workspace_id: $workspace_id}) RETURN w.name AS name", {"workspace_id": workspace_id})
        assert len(res_wf_nodes) > 0, "Expected Workflow node in Neo4j"
        
    # 5. Retrieve Context via temporal/how-to trigger
    print("\nStep 4: Retrieving context with procedural 'how to' query...")
    req_retrieve = MemoryRetrieve(
        user_id=user_id,
        workspace_id=workspace_id,
        query="How to deploy the application?",
        limit=5
    )
    res_retrieve = asyncio.run(retrieve_context(req_retrieve, format="markdown"))
    markdown_context = res_retrieve["markdown"]
    print("\n  Markdown Context returned:")
    print(markdown_context)
    
    # Assert recipe sections and steps are present in the timeline
    assert "## Procedural Recipes" in markdown_context
    assert "Recipe: deploy application" in markdown_context
    assert "1. Build docker image locally" in markdown_context
    assert "2. Push to Railway registry" in markdown_context
    assert "3. Sync Postgres migration scripts" in markdown_context
    
    conn.close()
    print("\n" + "=" * 60)
    print("  Procedural Memory & Workflow tests completed successfully!")
    print("=" * 60)

if __name__ == "__main__":
    test_procedural_memory_system()
