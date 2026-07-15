import uuid
import json
import time
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional

from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn, _mock_graph_data
from memoryos.services.background import ALLOWED_RELATIONSHIPS, insert_to_dlq

_is_replaying = False

def log_event(user_id: str, workspace_id: str, event_type: str, payload: dict):
    """Logs a state mutation event to the event_store table."""
    global _is_replaying
    if _is_replaying:
        return
    
    event_id = str(uuid.uuid4())
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_store (id, user_id, workspace_id, event_type, payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (event_id, user_id, workspace_id, event_type, json.dumps(payload))
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[EventStore] Failed to log event: {e}")

# ──────────────────────────────────────────────────────────────────────
# Background Job Helpers
# ──────────────────────────────────────────────────────────────────────

def create_job(job_id: str, job_type: str, user_id: str, workspace_id: str):
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO background_jobs (id, job_type, user_id, workspace_id, status)
            VALUES (%s, %s, %s, %s, 'QUEUED')
            """,
            (job_id, job_type, user_id, workspace_id)
        )
    conn.commit()
    conn.close()

def update_job_status(job_id: str, status: str, total_events: int = 0, processed_events: int = 0, error_message: Optional[str] = None):
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE background_jobs
            SET status = %s, total_events = %s, processed_events = %s, error_message = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
            """,
            (status, total_events, processed_events, error_message, job_id)
        )
    conn.commit()
    conn.close()

# ──────────────────────────────────────────────────────────────────────
# Precomputed Restoration Helpers
# ──────────────────────────────────────────────────────────────────────

def restore_precomputed_clauses(user_id: str, session_id: Optional[str], workspace_id: str, clauses: list):
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
        if session_id:
            cur.execute(
                "INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (session_id, user_id)
            )
            
        for item in clauses:
            evt = item["content"]
            embedding = item["embedding"]
            memory_type = item["memory_type"]
            importance = item["importance"]
            
            evt_clean = evt.lower().strip()
            raw_fp = f"{user_id}:{workspace_id}:{evt_clean}"
            fingerprint = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()
            
            evt_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO memories (id, user_id, session_id, workspace_id, content, embedding, memory_type, importance_score, fingerprint)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (evt_id, user_id, session_id, workspace_id, evt, embedding, memory_type, importance, fingerprint)
            )
    conn.commit()
    conn.close()

def restore_precomputed_graph(user_id: str, workspace_id: str, payload: dict):
    neo4j = get_neo4j_conn()
    if not neo4j:
        return
        
    entities = payload.get("entities", [])
    relationships = payload.get("relationships", [])
    
    is_mock = getattr(neo4j, "is_mock", False)
    if is_mock:
        # Populate Mock Graph data
        for ent in entities:
            name = ent["name"]
            _mock_graph_data["entities"][name] = {
                "name": name,
                "type": ent.get("type", "Entity"),
                "workspace": workspace_id
            }
        for rel in relationships:
            _mock_graph_data["relationships"].append({
                "source": rel["source"],
                "target": rel["target"],
                "type": rel["type"],
                "workspace_id": workspace_id
            })
        return
        
    neo4j.query("MERGE (u:User {id: $user_id, workspace_id: $workspace_id})", {
        "user_id": user_id, "workspace_id": workspace_id
    })
    
    for ent in entities:
        neo4j.query(
            "MERGE (e:Entity {name: $name, workspace_id: $workspace_id}) SET e.type = $type",
            {"name": ent["name"], "type": ent.get("type", "Entity"), "workspace_id": workspace_id}
        )
        neo4j.query(
            """
            MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
            MATCH (e:Entity {name: $name, workspace_id: $workspace_id})
            MERGE (u)-[r:KNOWS_ABOUT]->(e)
            """,
            {"user_id": user_id, "name": ent["name"], "workspace_id": workspace_id}
        )
        
    timestamp_str = datetime.now(timezone.utc).isoformat()
    for rel in relationships:
        rel_type = rel["type"].upper().strip()
        if not re.match(r"^[A-Z][A-Z0-9_]*$", rel_type):
            continue
        if rel_type not in ALLOWED_RELATIONSHIPS:
            rel_type = "RELATED_TO"
            
        neo4j.query(
            f"""
            MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}})
            MATCH (t:Entity {{name: $target, workspace_id: $workspace_id}})
            MERGE (s)-[r:{rel_type}]->(t)
            ON CREATE SET 
                r.version = 1,
                r.evidence_count = 1,
                r.created_at = $timestamp,
                r.valid_from = $timestamp,
                r.valid_to = null,
                r.workspace_id = $workspace_id,
                r.is_active = true
            ON MATCH SET 
                r.version = coalesce(r.version, 1) + 1,
                r.is_active = true
            """,
            {
                "source": rel["source"],
                "target": rel["target"],
                "workspace_id": workspace_id,
                "timestamp": timestamp_str
            }
        )

# ──────────────────────────────────────────────────────────────────────
# Replay vs Rebuild Workers
# ──────────────────────────────────────────────────────────────────────

async def replay_events(job_id: str, user_id: str, workspace_id: str = "default"):
    """
    Chronologically restores derived memory structures using the pre-computed payload logged inside the event.
    Deterministic, fast, and does not invoke external ML Models.
    """
    global _is_replaying
    _is_replaying = True
    update_job_status(job_id, "RUNNING")
    
    try:
        # 1. Purge SQL structures
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
        
        # 2. Purge Neo4j structures
        neo4j = get_neo4j_conn()
        if neo4j:
            is_mock = getattr(neo4j, "is_mock", False)
            if is_mock:
                entities_to_keep = {k: v for k, v in _mock_graph_data["entities"].items() if v.get("workspace") != workspace_id}
                _mock_graph_data["entities"] = entities_to_keep
                rels_to_keep = [r for r in _mock_graph_data["relationships"] if r.get("workspace_id") != workspace_id]
                _mock_graph_data["relationships"] = rels_to_keep
            else:
                neo4j.query("MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n", {"workspace_id": workspace_id})
                
        # Clear STM & Working Memory caches
        from memoryos.core.cache import stm_cache
        from memoryos.core.working_memory import working_memory
        stm_cache.clear(user_id, workspace_id)
        working_memory.clear_register(user_id, workspace_id)
        
        # Fetch events chronologically
        events = []
        conn = get_postgres_conn()
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT event_type, payload
                FROM event_store
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY created_at ASC
                """,
                (user_id, workspace_id)
            )
            events = cur.fetchall()
        conn.close()
        
        total_events = len(events)
        update_job_status(job_id, "RUNNING", total_events=total_events, processed_events=0)
        
        from memoryos.api.memories import trigger_consolidation, apply_decay
        from memoryos.schemas.memory import MemoryReflect
        
        processed_count = 0
        for event in events:
            event_type = event["event_type"]
            payload_data = event["payload"]
            if isinstance(payload_data, str):
                payload_data = json.loads(payload_data)
                
            if event_type == "MEMORY_INGESTED":
                clauses = payload_data.get("clauses", [])
                session_id = payload_data.get("session_id")
                # Direct SQL restore of pre-computed embeddings and types
                restore_precomputed_clauses(user_id, session_id, workspace_id, clauses)
                # Direct Neo4j restore of pre-computed entities/relationships
                restore_precomputed_graph(user_id, workspace_id, payload_data)
            elif event_type == "WORKFLOW_INGESTED":
                # Restore workflow step data directly
                conn = get_postgres_conn()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO workflows (id, user_id, workspace_id, name, description, steps)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (str(uuid.uuid4()), user_id, workspace_id, payload_data["name"], payload_data.get("description"), json.dumps(payload_data["steps"]))
                    )
                conn.commit()
                conn.close()
            elif event_type == "MEMORIES_CONSOLIDATED":
                data_obj = MemoryReflect(**payload_data)
                await trigger_consolidation(data_obj)
            elif event_type == "MEMORY_DECAYED":
                await apply_decay()
                
            processed_count += 1
            update_job_status(job_id, "RUNNING", total_events=total_events, processed_events=processed_count)
            
        update_job_status(job_id, "COMPLETED", total_events=total_events, processed_events=processed_count)
        logger.info(f"[ReplayEngine] Deterministic state reconstruction completed successfully for {user_id}/{workspace_id}.")
        
    except Exception as err:
        logger.error(f"[ReplayEngine] Replay failed: {err}")
        update_job_status(job_id, "FAILED", error_message=str(err))
        insert_to_dlq(user_id, workspace_id, "REPLAY_JOB", {"job_id": job_id}, str(err))
    finally:
        _is_replaying = False

async def rebuild_events(job_id: str, user_id: str, workspace_id: str = "default"):
    """
    Chronologically re-calculates all state mutations by re-extracting entities/relations and
    re-generating embeddings based on raw input text payloads. Used during model/migration upgrades.
    """
    global _is_replaying
    _is_replaying = True
    update_job_status(job_id, "RUNNING")
    
    try:
        # 1. Purge SQL structures
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
        
        # 2. Purge Neo4j structures
        neo4j = get_neo4j_conn()
        if neo4j:
            is_mock = getattr(neo4j, "is_mock", False)
            if is_mock:
                entities_to_keep = {k: v for k, v in _mock_graph_data["entities"].items() if v.get("workspace") != workspace_id}
                _mock_graph_data["entities"] = entities_to_keep
                rels_to_keep = [r for r in _mock_graph_data["relationships"] if r.get("workspace_id") != workspace_id]
                _mock_graph_data["relationships"] = rels_to_keep
            else:
                neo4j.query("MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n", {"workspace_id": workspace_id})
                
        # Clear STM & Working Memory caches
        from memoryos.core.cache import stm_cache
        from memoryos.core.working_memory import working_memory
        stm_cache.clear(user_id, workspace_id)
        working_memory.clear_register(user_id, workspace_id)
        
        # Fetch events chronologically
        events = []
        conn = get_postgres_conn()
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT event_type, payload
                FROM event_store
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY created_at ASC
                """,
                (user_id, workspace_id)
            )
            events = cur.fetchall()
        conn.close()
        
        total_events = len(events)
        update_job_status(job_id, "RUNNING", total_events=total_events, processed_events=0)
        
        from memoryos.services.ingestion import MemoryIngestionService
        from memoryos.schemas.memory import MemoryIngest, WorkflowIngest, MemoryReflect
        from memoryos.api.memories import ingest_workflow, trigger_consolidation, apply_decay
        
        processed_count = 0
        for event in events:
            event_type = event["event_type"]
            payload_data = event["payload"]
            if isinstance(payload_data, str):
                payload_data = json.loads(payload_data)
                
            if event_type == "MEMORY_INGESTED":
                # Re-ingest raw text (generating new embeddings and extracting entities/relations)
                raw_text = payload_data.get("raw_text") or payload_data.get("content")
                session_id = payload_data.get("session_id")
                data_obj = MemoryIngest(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    content=raw_text,
                    session_id=session_id
                )
                # Pass background_tasks = None to run graph extraction synchronously in chronological order
                await MemoryIngestionService.ingest(data_obj, background_tasks=None)
            elif event_type == "WORKFLOW_INGESTED":
                data_obj = WorkflowIngest(**payload_data)
                await ingest_workflow(data_obj)
            elif event_type == "MEMORIES_CONSOLIDATED":
                data_obj = MemoryReflect(**payload_data)
                await trigger_consolidation(data_obj)
            elif event_type == "MEMORY_DECAYED":
                await apply_decay()
                
            processed_count += 1
            update_job_status(job_id, "RUNNING", total_events=total_events, processed_events=processed_count)
            
        update_job_status(job_id, "COMPLETED", total_events=total_events, processed_events=processed_count)
        logger.info(f"[RebuildEngine] Full recalculation/rebuild state completed successfully for {user_id}/{workspace_id}.")
        
    except Exception as err:
        logger.error(f"[RebuildEngine] Rebuild failed: {err}")
        update_job_status(job_id, "FAILED", error_message=str(err))
        insert_to_dlq(user_id, workspace_id, "REBUILD_JOB", {"job_id": job_id}, str(err))
    finally:
        _is_replaying = False
