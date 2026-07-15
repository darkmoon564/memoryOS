import uuid
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional
from fastapi import BackgroundTasks, HTTPException

from memoryos.config import logger
from memoryos.schemas.memory import MemoryIngest, IngestResponse
from memoryos.core.event_parser import parse_events
from memoryos.core.classifier import classify_memory
from memoryos.core.scorer import calculate_importance
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.services.background import background_graph_ingest
from memoryos.db.postgres import get_postgres_conn
from memoryos.core.event_store import log_event
from memoryos.core.cache import stm_cache
from memoryos.api.memories import get_embedding_model, process_conversation_log

class MemoryIngestionService:
    """Centralized service managing validation, classification, SQL/Graph ingestion, and event logging."""
    
    @staticmethod
    async def ingest(data: MemoryIngest, background_tasks: Optional[BackgroundTasks] = None) -> IngestResponse:
        try:
            events = parse_events(data.content)
        except Exception as e:
            logger.error(f"Event parser failed: {e}. Falling back to raw content.")
            events = [data.content]
            
        if not events:
            events = [data.content]
            
        try:
            model = get_embedding_model()
        except Exception as e:
            logger.error(f"Embedding model initialization failed: {e}")
            raise HTTPException(status_code=500, detail="Embedding model execution failed to initialize.")
            
        # 1. Pre-calculate all derived payloads for event logging (determinstic replay storage)
        clauses_payload = []
        for evt in events:
            importance = calculate_importance(evt)
            memory_type = classify_memory(evt)
            try:
                emb_res = model.encode(evt)
                embedding = emb_res.tolist() if hasattr(emb_res, "tolist") else list(emb_res)
            except Exception as e:
                logger.error(f"Embedding generation failed: {e}")
                raise HTTPException(status_code=500, detail="Embedding model execution failed.")
            
            clauses_payload.append({
                "content": evt,
                "embedding": embedding,
                "memory_type": memory_type,
                "importance": importance
            })
            
        # Extract graph structures for the event store payload
        graph_data = extract_entities_and_relationships(data.content)
        
        event_payload = {
            "raw_text": data.content,
            "session_id": data.session_id,
            "clauses": clauses_payload,
            "entities": graph_data.get("entities", []),
            "relationships": graph_data.get("relationships", []),
            "metadata": {
                "schema_version": "1.2",
                "embedding_model": "all-MiniLM-L6-v2",
                "extractor_version": "spacy_fallback"
            }
        }
        
        # Log event with full computed payload
        log_event(data.user_id, data.workspace_id, "MEMORY_INGESTED", event_payload)
        
        # 2. Persist to PostgreSQL
        ingested_memory_ids = []
        try:
            conn = get_postgres_conn()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (data.user_id,))
                if data.session_id:
                    cur.execute(
                        "INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                        (data.session_id, data.user_id)
                    )
                
                # Build temporal episodes
                try:
                    process_conversation_log(
                        conn,
                        data.user_id,
                        data.session_id,
                        data.workspace_id,
                        data.content,
                        model
                    )
                except Exception as ep_err:
                    logger.error(f"Episode builder/logging failed: {ep_err}")
                
                # Write individual clauses
                for item in clauses_payload:
                    evt_id = str(uuid.uuid4())
                    evt = item["content"]
                    embedding = item["embedding"]
                    memory_type = item["memory_type"]
                    importance = item["importance"]
                    
                    evt_clean = evt.lower().strip()
                    raw_fp = f"{data.user_id}:{data.workspace_id}:{evt_clean}"
                    fingerprint = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()
                    
                    existing_memory_id = None
                    frequency_updated = False
                    
                    cur.execute(
                        "SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s AND fingerprint = %s AND is_active = TRUE",
                        (data.user_id, data.workspace_id, fingerprint)
                    )
                    row = cur.fetchone()
                    if row:
                        existing_memory_id = str(row[0]) if not isinstance(row, dict) else str(row['id'])
                        cur.execute(
                            "UPDATE memories SET frequency_count = frequency_count + 1, last_accessed_at = CURRENT_TIMESTAMP WHERE id = %s",
                            (existing_memory_id,)
                        )
                        frequency_updated = True
                    else:
                        cur.execute(
                            """
                            INSERT INTO memories (id, user_id, session_id, workspace_id, content, embedding, memory_type, importance_score, fingerprint)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (evt_id, data.user_id, data.session_id, data.workspace_id, evt, embedding, memory_type, importance, fingerprint)
                        )
                    
                    target_memory_id = existing_memory_id if frequency_updated else evt_id
                    ingested_memory_ids.append((target_memory_id, evt, embedding, memory_type, frequency_updated))
            
            conn.commit()
            conn.close()
        except HTTPException as he:
            raise he
        except Exception as e:
            logger.error(f"Postgres write failed: {e}")
            raise HTTPException(status_code=500, detail="Database write error.")
            
        # 3. Graph Ingestion
        for target_memory_id, evt, embedding, memory_type, frequency_updated in ingested_memory_ids:
            if background_tasks:
                background_tasks.add_task(
                    background_graph_ingest,
                    target_memory_id,
                    evt,
                    data.user_id,
                    data.workspace_id
                )
            else:
                # Synchronous graph ingestion (for CLI / tests / Replay context)
                background_graph_ingest(
                    target_memory_id,
                    evt,
                    data.user_id,
                    data.workspace_id
                )
                
            stm_cache.push(data.user_id, data.workspace_id, target_memory_id, evt, embedding)
            
        first_id = ingested_memory_ids[0][0] if ingested_memory_ids else ""
        first_type = ingested_memory_ids[0][3] if ingested_memory_ids else "UNKNOWN"
        is_updated = ingested_memory_ids[0][4] if ingested_memory_ids else False
        
        count = len(events)
        message = (
            f"Memory successfully parsed into {count} atomic events. "
            f"Primary memory ({first_type}) {'updated' if is_updated else 'ingested'} and queued for indexing."
        )
        
        return IngestResponse(
            status="success",
            memory_id=first_id,
            message=message
        )
