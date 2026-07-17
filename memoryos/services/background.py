import re
import time
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from psycopg2.extras import RealDictCursor
from memoryos.config import logger
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.core.contradiction import resolve_contradictions
from memoryos.core.entity_resolver import resolve_entity
from memoryos.db.postgres import get_postgres_conn
from memoryos.observability import metrics

ALLOWED_RELATIONSHIPS = {
    "WORKS_AT", "LIVES_IN", "INTERESTED_IN", "USES", "KNOWS", "OWNS",
    "LEARNING_TOPIC", "BELONGS_TO_TOPIC", "HAS_PROFILE", "BELONGS_TO_PROFILE",
    "HAS_WORKFLOW", "USES_TECH", "KNOWS_ABOUT", "SUPERSEDED_BY"
}
MAX_GRAPH_PROJECTION_ATTEMPTS = 10
GRAPH_PROJECTION_LEASE_SECONDS = 300
GRAPH_PROJECTION_MAX_BACKOFF_SECONDS = 300

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
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
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
        metrics.increment("memoryos_dead_letter_events_total")
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
            return False

    # 2. Retry Ingestion Loop
    max_retries = 3
    retry_delay = 1.0
    
    for attempt in range(1, max_retries + 1):
        try:
            _execute_graph_inserts(memory_id, user_id, workspace_id, graph_data)
            return True
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
                return False


def process_graph_projection(projection_id: str) -> bool:
    """Atomically claim and project one durable graph-outbox row."""
    conn = get_postgres_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # RETURNING makes the claim single-consumer even when multiple API
            # workers wake up at the same time.
            cur.execute(
                """
                UPDATE graph_projection_outbox
                SET status = 'PROCESSING', attempts = attempts + 1, error_message = NULL,
                    locked_at = CURRENT_TIMESTAMP
                WHERE id = %s AND (
                    (status IN ('PENDING', 'RETRY') AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP))
                    OR (status = 'PROCESSING' AND locked_at < %s)
                )
                RETURNING memory_id, user_id, workspace_id, content, graph_payload, attempts
                """,
                (projection_id, datetime.now(timezone.utc) - timedelta(seconds=GRAPH_PROJECTION_LEASE_SECONDS)),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return False

        row_data = dict(row)
        payload = row_data["graph_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        try:
            _execute_graph_inserts(
                str(row_data["memory_id"]),
                row_data["user_id"],
                row_data["workspace_id"],
                payload,
            )
        except Exception as exc:
            error_message = str(exc)
            if row_data["attempts"] >= MAX_GRAPH_PROJECTION_ATTEMPTS:
                _finish_graph_projection(projection_id, "FAILED", error_message)
                insert_to_dlq(
                    row_data["user_id"],
                    row_data["workspace_id"],
                    "GRAPH_PROJECTION_FAILED",
                    {"projection_id": projection_id, "memory_id": str(row_data["memory_id"]), "graph_payload": payload},
                    error_message,
                )
                logger.exception("[Graph Outbox] Projection %s exhausted retries and moved to DLQ", projection_id)
                metrics.increment("memoryos_graph_projections_total", {"outcome": "failed"})
            else:
                retry_delay = min(GRAPH_PROJECTION_MAX_BACKOFF_SECONDS, 2 ** row_data["attempts"])
                _finish_graph_projection(
                    projection_id,
                    "RETRY",
                    error_message,
                    next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=retry_delay),
                )
                logger.exception("[Graph Outbox] Projection %s failed and will be retried", projection_id)
                metrics.increment("memoryos_graph_projections_total", {"outcome": "retry"})
            return False

        _finish_graph_projection(projection_id, "COMPLETED")
        metrics.increment("memoryos_graph_projections_total", {"outcome": "completed"})
        return True
    finally:
        conn.close()


def _finish_graph_projection(projection_id: str, status: str, error_message: str | None = None, next_attempt_at=None) -> None:
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE graph_projection_outbox
                SET status = %s, error_message = %s,
                    completed_at = CASE WHEN %s = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE NULL END,
                    next_attempt_at = %s,
                    locked_at = NULL
                WHERE id = %s
                """,
                (status, error_message, status, next_attempt_at, projection_id),
            )
        conn.commit()
    finally:
        conn.close()


def drain_graph_projections(limit: int = 100) -> int:
    """Recover queued graph work after startup and on the periodic worker tick."""
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM graph_projection_outbox
                WHERE (status IN ('PENDING', 'RETRY') AND (next_attempt_at IS NULL OR next_attempt_at <= CURRENT_TIMESTAMP))
                   OR (status = 'PROCESSING' AND locked_at < %s)
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (datetime.now(timezone.utc) - timedelta(seconds=GRAPH_PROJECTION_LEASE_SECONDS), limit),
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    completed = 0
    for row in rows:
        projection_id = row["id"] if isinstance(row, dict) else row[0]
        if process_graph_projection(str(projection_id)):
            completed += 1
    return completed

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
