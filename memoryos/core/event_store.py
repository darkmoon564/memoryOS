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
from memoryos.services.background import insert_to_dlq

_is_replaying = False

def log_event(user_id: str, workspace_id: str, event_type: str, payload: dict, conn=None):
    """Log a mutation event, optionally in the caller's database transaction."""
    global _is_replaying
    if _is_replaying:
        return
    
    event_id = str(uuid.uuid4())
    owns_connection = conn is None
    try:
        if owns_connection:
            conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO event_store (id, user_id, workspace_id, event_type, payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (event_id, user_id, workspace_id, event_type, json.dumps(payload))
            )
        if owns_connection:
            conn.commit()
    except Exception as e:
        if owns_connection and conn:
            conn.rollback()
        logger.error(f"[EventStore] Failed to log event: {e}")
        raise
    finally:
        if owns_connection and conn:
            conn.close()

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

def _parse_event_time(value) -> datetime:
    if isinstance(value, datetime):
        event_time = value
    elif isinstance(value, str):
        try:
            event_time = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            event_time = datetime.now(timezone.utc)
    else:
        event_time = datetime.now(timezone.utc)
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)
    return event_time.astimezone(timezone.utc)


def restore_precomputed_clauses(
    user_id: str,
    session_id: Optional[str],
    workspace_id: str,
    clauses: list,
    occurred_at=None,
    source_event_id: Optional[str] = None,
):
    """Restore canonical SQL state and queue graph work from stored event data.

    Replay follows the live ingestion deduplication contract. It never writes
    directly to Neo4j, so a graph outage after a replay is recovered by the
    same durable outbox worker used for regular requests.
    """
    default_event_time = _parse_event_time(occurred_at)
    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (user_id,))
            if session_id:
                cur.execute(
                    "INSERT INTO sessions (id, user_id, started_at) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (session_id, user_id, default_event_time),
                )

            for item in clauses:
                evt = item["content"]
                embedding = item["embedding"]
                memory_type = item["memory_type"]
                importance = item["importance"]
                item_event_time = _parse_event_time(item.get("occurred_at") or default_event_time)
                item_source_id = item.get("source_event_id") or source_event_id

                evt_clean = evt.lower().strip()
                raw_fp = f"{user_id}:{workspace_id}:{evt_clean}"
                fingerprint = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()

                cur.execute(
                    "SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s AND fingerprint = %s AND is_active = TRUE",
                    (user_id, workspace_id, fingerprint),
                )
                row = cur.fetchone()
                source_is_new = True
                if row:
                    memory_id = str(row[0]) if not isinstance(row, dict) else str(row["id"])
                    if item_source_id:
                        cur.execute(
                            "SELECT 1 FROM memory_sources WHERE memory_id = %s AND source_event_id = %s",
                            (memory_id, item_source_id),
                        )
                        source_is_new = cur.fetchone() is None
                    if source_is_new:
                        cur.execute(
                            """
                            UPDATE memories
                            SET frequency_count = frequency_count + 1,
                                last_accessed_at = CURRENT_TIMESTAMP,
                                occurred_at = CASE WHEN occurred_at < %s THEN %s ELSE occurred_at END
                            WHERE id = %s
                            """,
                            (item_event_time, item_event_time, memory_id),
                        )
                else:
                    memory_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO memories
                            (id, user_id, session_id, workspace_id, content, embedding, memory_type,
                             importance_score, fingerprint, occurred_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (memory_id, user_id, session_id, workspace_id, evt, embedding, memory_type, importance, fingerprint, item_event_time),
                    )

                if item_source_id and source_is_new:
                    cur.execute(
                        """
                        INSERT INTO memory_sources (memory_id, source_event_id, occurred_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (memory_id, source_event_id)
                        DO UPDATE SET occurred_at = EXCLUDED.occurred_at
                        """,
                        (memory_id, item_source_id, item_event_time),
                    )

                graph_payload = {
                    "entities": item.get("entities", []),
                    "relationships": item.get("relationships", []),
                    "occurred_at": item_event_time.isoformat(),
                    "source_event_id": item_source_id,
                }
                if source_is_new or not item_source_id:
                    cur.execute(
                        """
                        INSERT INTO graph_projection_outbox
                            (id, memory_id, user_id, workspace_id, content, graph_payload)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (str(uuid.uuid4()), memory_id, user_id, workspace_id, evt, json.dumps(graph_payload)),
                    )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

# ──────────────────────────────────────────────────────────────────────
def execute_workflow_ingest(user_id: str, workspace_id: str, name: str, description: Optional[str], steps: list) -> str:
    """Saves workflow procedural step data in postgres/neo4j directly without HTTP route endpoints."""
    workflow_id = str(uuid.uuid4())
    conn = get_postgres_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflows (id, user_id, workspace_id, name, description, steps)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (workflow_id, user_id, workspace_id, name, description, json.dumps(steps))
        )
    conn.commit()
    conn.close()

    neo4j = get_neo4j_conn()
    if neo4j:
        is_mock = getattr(neo4j, "is_mock", False)
        if is_mock:
            _mock_graph_data["users"][user_id] = {"workspace": workspace_id}
            _mock_graph_data["entities"][name] = {
                "name": name,
                "type": "Workflow",
                "workspace": workspace_id,
                "user_id": user_id,
                "description": description
            }
            _mock_graph_data["relationships"].append({
                "source": user_id,
                "target": name,
                "type": "HAS_WORKFLOW",
                "workspace_id": workspace_id,
                "user_id": user_id,
                "is_active": True
            })
        else:
            neo4j.query(
                "MERGE (u:User {id: $user_id, workspace_id: $workspace_id})",
                {"user_id": user_id, "workspace_id": workspace_id},
            )
            neo4j.query(
                """
                MERGE (w:Workflow {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                SET w.description = $description
                """,
                {"name": name, "description": description, "workspace_id": workspace_id, "user_id": user_id}
            )
            neo4j.query(
                """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (w:Workflow {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                MERGE (u)-[r:HAS_WORKFLOW {user_id: $user_id}]->(w)
                SET r.workspace_id = $workspace_id, r.is_active = true
                """,
                {"user_id": user_id, "name": name, "workspace_id": workspace_id}
            )
            
        tech_keywords = ["docker", "postgres", "sqlite", "neovim", "python", "rust", "railway", "github", "git"]
        detected_techs = set()
        for step in steps:
            step_lower = step.lower()
            for tech in tech_keywords:
                if tech in step_lower:
                    detected_techs.add(tech)
                    
        for tech in detected_techs:
            if is_mock:
                _mock_graph_data["entities"].setdefault(tech, {
                    "name": tech,
                    "type": "Technology",
                    "workspace": workspace_id,
                    "user_id": user_id,
                })
                _mock_graph_data["relationships"].append({
                    "source": name,
                    "target": tech,
                    "type": "USES_TECH",
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "is_active": True
                })
            else:
                neo4j.query(
                    """
                    MERGE (t:Entity {name: $tech, workspace_id: $workspace_id, user_id: $user_id})
                    SET t.type = 'Technology'
                    """,
                    {"tech": tech, "workspace_id": workspace_id, "user_id": user_id},
                )
                neo4j.query(
                    """
                    MATCH (t:Entity {name: $tech, workspace_id: $workspace_id, user_id: $user_id})
                    MATCH (w:Workflow {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                    MERGE (w)-[r:USES_TECH {user_id: $user_id}]->(t)
                    SET r.workspace_id = $workspace_id, r.is_active = true
                    """,
                    {"tech": tech, "name": name, "workspace_id": workspace_id, "user_id": user_id}
                )
    return workflow_id

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
            cur.execute("SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            memory_ids = [str(row[0]) for row in cur.fetchall()]
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
        
        # 2. Purge only this user's derived graph structures.
        from memoryos.services.background import _delete_user_graph_data
        _delete_user_graph_data(user_id, workspace_id, memory_ids)
                
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
                restore_precomputed_clauses(
                    user_id,
                    session_id,
                    workspace_id,
                    clauses,
                    occurred_at=payload_data.get("occurred_at"),
                    source_event_id=payload_data.get("source_event_id"),
                )
            elif event_type == "WORKFLOW_INGESTED":
                # Restore workflow data directly
                execute_workflow_ingest(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    name=payload_data["name"],
                    description=payload_data.get("description"),
                    steps=payload_data["steps"]
                )
            elif event_type == "MEMORIES_CONSOLIDATED":
                from memoryos.core.consolidation import consolidate_hierarchy
                neo4j = get_neo4j_conn()
                if neo4j:
                    consolidate_hierarchy(user_id, workspace_id, neo4j)
            elif event_type == "MEMORY_DECAYED":
                from memoryos.core.scorer import _execute_decay_logic
                _execute_decay_logic(workspace_id)
                
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
            cur.execute("SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            memory_ids = [str(row[0]) for row in cur.fetchall()]
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
        
        # 2. Purge only this user's derived graph structures.
        from memoryos.services.background import _delete_user_graph_data
        _delete_user_graph_data(user_id, workspace_id, memory_ids)
                
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
                    session_id=session_id,
                    occurred_at=payload_data.get("occurred_at"),
                    source_event_id=payload_data.get("source_event_id"),
                )
                # Pass background_tasks = None to run graph extraction synchronously in chronological order
                await MemoryIngestionService.ingest(data_obj, background_tasks=None)
            elif event_type == "WORKFLOW_INGESTED":
                execute_workflow_ingest(
                    user_id=user_id,
                    workspace_id=workspace_id,
                    name=payload_data["name"],
                    description=payload_data.get("description"),
                    steps=payload_data["steps"]
                )
            elif event_type == "MEMORIES_CONSOLIDATED":
                from memoryos.core.consolidation import consolidate_hierarchy
                neo4j = get_neo4j_conn()
                if neo4j:
                    consolidate_hierarchy(user_id, workspace_id, neo4j)
            elif event_type == "MEMORY_DECAYED":
                from memoryos.core.scorer import _execute_decay_logic
                _execute_decay_logic(workspace_id)
                
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
