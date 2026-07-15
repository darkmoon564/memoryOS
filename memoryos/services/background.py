import re
import time
import json
import uuid
from datetime import datetime, timezone
from typing import Optional
from memoryos.config import logger
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.core.contradiction import resolve_contradictions
from memoryos.core.entity_resolver import resolve_entity

ALLOWED_RELATIONSHIPS = {
    "WORKS_AT", "LIVES_IN", "INTERESTED_IN", "USES", "KNOWS", "OWNS",
    "LEARNING_TOPIC", "BELONGS_TO_TOPIC", "HAS_PROFILE", "BELONGS_TO_PROFILE",
    "HAS_WORKFLOW", "USES_TECH", "KNOWS_ABOUT", "SUPERSEDED_BY"
}

def sanitize_relationship_type(rel_type: str) -> Optional[str]:
    """Validates the relationship type string for Cypher safety and whitelists."""
    rel_type = rel_type.upper().strip()
    if not re.match(r"^[A-Z][A-Z0-9_]*$", rel_type):
        logger.warning(f"[Graph Safety] Discarding invalid relationship string: '{rel_type}' (Cypher injection block)")
        return None
    if rel_type not in ALLOWED_RELATIONSHIPS:
        logger.info(f"[Graph Safety] Relationship '{rel_type}' not whitelisted. Mapping to RELATED_TO.")
        return "RELATED_TO"
    return rel_type

def insert_to_dlq(user_id: str, workspace_id: str, event_type: str, payload: dict, error_message: str):
    """Inserts a failed task payload into the dead_letter_queue table."""
    try:
        from memoryos.db.postgres import get_postgres_conn
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO dead_letter_queue (id, user_id, workspace_id, event_type, payload, error_message)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (str(uuid.uuid4()), user_id, workspace_id, event_type, json.dumps(payload), error_message)
            )
        conn.commit()
        conn.close()
        logger.info(f"[DLQ] Logged failed job to dead_letter_queue table.")
    except Exception as dlq_err:
        logger.error(f"[DLQ] Failed to write to dead_letter_queue table: {dlq_err}")

def background_graph_ingest(memory_id: str, content: str, user_id: str, workspace_id: str, precomputed_graph: dict = None):
    """Background task to extract entities/relations and update the Neo4j Graph with retry logic and DLQ fallback."""
    logger.info(f"Triggering entity extraction for memory: {memory_id}")
    
    # 1. Extract Entities (skip if precomputed graph data is provided)
    if precomputed_graph is not None:
        graph_data = precomputed_graph
    else:
        try:
            graph_data = extract_entities_and_relationships(content)
        except Exception as e:
            logger.error(f"[Graph Ingest] Extraction failed: {e}")
            insert_to_dlq(user_id, workspace_id, "MEMORY_INGESTED", {
                "memory_id": memory_id, "content": content
            }, f"Extraction failed: {e}")
            from memoryos.core import event_store
            if event_store._is_replaying:
                raise e
            return

    # 2. Retry Ingestion Loop
    max_retries = 3
    retry_delay = 1.0
    
    for attempt in range(1, max_retries + 1):
        try:
            _execute_graph_inserts(memory_id, user_id, workspace_id, graph_data)
            return  # Success
        except Exception as e:
            logger.warning(f"[Graph Ingest] Insertion attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                time.sleep(retry_delay)
                retry_delay *= 2.0
            else:
                logger.error(f"[Graph Ingest] Insertion failed permanently: {e}")
                insert_to_dlq(user_id, workspace_id, "MEMORY_INGESTED", {
                    "memory_id": memory_id, "content": content, "graph_data": graph_data
                }, f"Graph insertion failed after {max_retries} retries: {e}")
                from memoryos.core import event_store
                if event_store._is_replaying:
                    raise e

def _execute_graph_inserts(memory_id: str, user_id: str, workspace_id: str, graph_data: dict):
    """Executes the raw Cypher query database modifications on Neo4j."""
    neo4j = get_neo4j_conn()
    if not neo4j:
        raise ConnectionError("Neo4j connector not initialized or unavailable.")
        
    entities = graph_data.get("entities", [])
    relationships = graph_data.get("relationships", [])
    
    # Resolve aliases
    resolved_map = {}
    for entity in entities:
        raw_name = entity["name"]
        resolved_map[raw_name] = resolve_entity(raw_name, workspace_id)
        
    for rel in relationships:
        src = rel["source"]
        tgt = rel["target"]
        if src not in resolved_map:
            resolved_map[src] = resolve_entity(src, workspace_id)
        if tgt not in resolved_map:
            resolved_map[tgt] = resolve_entity(tgt, workspace_id)
            
    # Insert User Node
    user_query = "MERGE (u:User {id: $user_id, workspace_id: $workspace_id})"
    neo4j.query(user_query, {"user_id": user_id, "workspace_id": workspace_id})
    
    # Create canonical entities
    for raw_name, canonical_name in resolved_map.items():
        ent_type = "Entity"
        for ent in entities:
            if ent["name"] == raw_name:
                ent_type = ent["type"]
                break
                
        ent_query = """
            MERGE (e:Entity {name: $name, workspace_id: $workspace_id})
            SET e.type = $type
        """
        neo4j.query(ent_query, {
            "name": canonical_name,
            "type": ent_type,
            "workspace_id": workspace_id
        })
        
        if raw_name != canonical_name:
            alias_query = "MERGE (a:Alias {name: $alias_name, workspace_id: $workspace_id})"
            neo4j.query(alias_query, {"alias_name": raw_name, "workspace_id": workspace_id})
            
            alias_rel_query = """
                MATCH (a:Alias {name: $alias_name, workspace_id: $workspace_id})
                MATCH (e:Entity {name: $canonical_name, workspace_id: $workspace_id})
                MERGE (a)-[r:ALIAS_OF]->(e)
            """
            neo4j.query(alias_rel_query, {
                "alias_name": raw_name,
                "canonical_name": canonical_name,
                "workspace_id": workspace_id
            })
            
        user_ent_query = """
            MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
            MATCH (e:Entity {name: $name, workspace_id: $workspace_id})
            MERGE (u)-[r:KNOWS_ABOUT]->(e)
        """
        neo4j.query(user_ent_query, {
            "user_id": user_id,
            "name": canonical_name,
            "workspace_id": workspace_id
        })
        
    resolved_rels = []
    for rel in relationships:
        src_canonical = resolved_map.get(rel["source"], rel["source"])
        tgt_canonical = resolved_map.get(rel["target"], rel["target"])
        if src_canonical == tgt_canonical:
            continue
        resolved_rels.append({
            "source": src_canonical,
            "target": tgt_canonical,
            "type": rel["type"]
        })
        
    resolve_contradictions(user_id, workspace_id, resolved_rels, neo4j)
    
    timestamp_str = datetime.now(timezone.utc).isoformat()
    for rel in resolved_rels:
        rel_type = sanitize_relationship_type(rel["type"])
        if not rel_type:
            continue
            
        rel_query = f"""
            MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}})
            MATCH (t:Entity {{name: $target, workspace_id: $workspace_id}})
            MERGE (s)-[r:{rel_type}]->(t)
            ON CREATE SET 
                r.version = 1,
                r.evidence_count = 1,
                r.created_at = $timestamp,
                r.valid_from = $timestamp,
                r.valid_to = null,
                r.source_memory_id = $source_memory_id,
                r.confidence = $confidence,
                r.workspace_id = $workspace_id,
                r.is_active = true
            ON MATCH SET 
                r.version = coalesce(r.version, 1) + 1,
                r.evidence_count = coalesce(r.evidence_count, 1) + (CASE WHEN r.source_memory_id <> $source_memory_id THEN 1 ELSE 0 END),
                r.updated_at = $timestamp,
                r.valid_from = coalesce(r.valid_from, $timestamp),
                r.valid_to = null,
                r.superseded_by = null,
                r.source_memory_id = $source_memory_id,
                r.confidence = $confidence,
                r.is_active = true
        """
        neo4j.query(rel_query, {
            "source": rel["source"],
            "target": rel["target"],
            "workspace_id": workspace_id,
            "source_memory_id": memory_id,
            "timestamp": timestamp_str,
            "confidence": float(rel.get("confidence", 0.9))
        })
        
    logger.info(f"Graph insertion completed successfully for memory: {memory_id}")
