import uuid
import json
import time
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.config import _mock_graph_data

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
        from memoryos.config import logger
        logger.error(f"[EventStore] Failed to log event: {e}")

async def replay_events(user_id: str, workspace_id: str = "default"):
    """
    Clears derived memory structures (PostgreSQL tables, vector indexes,
    and Neo4j graph nodes/relations) and sequentially replays all event logs
    in chronological order to rebuild state.
    """
    global _is_replaying
    _is_replaying = True
    
    from memoryos.config import logger
    logger.info(f"[EventStore] Initiating state replay for {user_id}/{workspace_id}...")
    
    # 1. Purge derived database tables
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[EventStore] Failed to purge derived SQL tables: {e}")
        
    # 2. Purge Neo4j graph structures
    neo4j = get_neo4j_conn()
    if neo4j:
        try:
            is_mock = getattr(neo4j, "is_mock", False)
            if is_mock:
                # Clear mock graph entities/relationships matching this workspace
                entities_to_keep = {k: v for k, v in _mock_graph_data["entities"].items() if v.get("workspace") != workspace_id}
                _mock_graph_data["entities"] = entities_to_keep
                
                rels_to_keep = [r for r in _mock_graph_data["relationships"] if r.get("workspace_id") != workspace_id]
                _mock_graph_data["relationships"] = rels_to_keep
            else:
                neo4j.query("MATCH (n {workspace_id: $workspace_id}) DETACH DELETE n", {"workspace_id": workspace_id})
        except Exception as e:
            logger.error(f"[EventStore] Failed to purge Neo4j graph: {e}")
            
    # Clear caches
    from memoryos.core.cache import stm_cache
    from memoryos.core.working_memory import working_memory
    stm_cache.clear(user_id, workspace_id)
    working_memory.clear_register(user_id, workspace_id)
    
    # 3. Fetch chronological log of events
    events = []
    try:
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
    except Exception as e:
        logger.error(f"[EventStore] Failed to fetch events: {e}")
        _is_replaying = False
        return
        
    # 4. Sequentially replay
    from memoryos.api.memories import ingest_memory, ingest_workflow, trigger_consolidation, apply_decay
    from memoryos.schemas.memory import MemoryIngest, WorkflowIngest, MemoryReflect
    
    for event in events:
        event_type = event["event_type"]
        payload_data = event["payload"]
        if isinstance(payload_data, str):
            payload_data = json.loads(payload_data)
            
        logger.info(f"[EventStore] Replaying event: {event_type}")
        try:
            if event_type == "MEMORY_INGESTED":
                from fastapi import BackgroundTasks
                data_obj = MemoryIngest(**payload_data)
                bg = BackgroundTasks()
                await ingest_memory(data_obj, bg)
            elif event_type == "WORKFLOW_INGESTED":
                data_obj = WorkflowIngest(**payload_data)
                await ingest_workflow(data_obj)
            elif event_type == "MEMORIES_CONSOLIDATED":
                data_obj = MemoryReflect(**payload_data)
                await trigger_consolidation(data_obj)
            elif event_type == "MEMORY_DECAYED":
                await apply_decay()
        except Exception as e:
            logger.error(f"[EventStore] Error replaying event {event_type}: {e}")
            
    _is_replaying = False
    logger.info(f"[EventStore] Replay completed for {user_id}/{workspace_id}.")
