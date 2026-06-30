from memoryos.config import logger
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.core.contradiction import resolve_contradictions

def background_graph_ingest(memory_id: str, content: str, user_id: str, workspace_id: str):
    """Background task to extract entities/relations and update the Neo4j Graph."""
    logger.info(f"Triggering entity extraction for memory: {memory_id}")
    
    graph_data = extract_entities_and_relationships(content)
    
    neo4j = get_neo4j_conn()
    if neo4j:
        # Resolve contradictions BEFORE inserting new relationships
        relationships = graph_data.get("relationships", [])
        resolve_contradictions(user_id, workspace_id, relationships, neo4j)
        
        # Insert User Node
        user_query = "MERGE (u:User {id: $user_id, workspace_id: $workspace_id})"
        neo4j.query(user_query, {"user_id": user_id, "workspace_id": workspace_id})
        
        # Create Entities
        for entity in graph_data.get("entities", []):
            ent_query = """
                MERGE (e:Entity {name: $name, workspace_id: $workspace_id})
                SET e.type = $type
            """
            neo4j.query(ent_query, {
                "name": entity["name"],
                "type": entity["type"],
                "workspace_id": workspace_id
            })
            
            # Connect User to Entities to establish context
            user_ent_query = """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (e:Entity {name: $name, workspace_id: $workspace_id})
                MERGE (u)-[r:KNOWS_ABOUT]->(e)
            """
            neo4j.query(user_ent_query, {
                "user_id": user_id,
                "name": entity["name"],
                "workspace_id": workspace_id
            })
            
        # Create Relationships between Entities
        for rel in relationships:
            rel_query = f"""
                MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}})
                MATCH (t:Entity {{name: $target, workspace_id: $workspace_id}})
                MERGE (s)-[r:{rel['type']}]->(t)
                SET r.confidence = 0.9, r.created_at = datetime(), r.is_active = true
            """
            neo4j.query(rel_query, {
                "source": rel["source"],
                "target": rel["target"],
                "workspace_id": workspace_id
            })
            
        logger.info(f"Graph ingestion completed for memory: {memory_id}")
