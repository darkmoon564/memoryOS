import uuid
import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Optional
from fastapi import BackgroundTasks, HTTPException

from memoryos.config import logger
from memoryos.schemas.memory import MemoryIngest, IngestResponse
from memoryos.core.event_parser import parse_events
from memoryos.core.classifier import classify_memory
from memoryos.core.scorer import calculate_importance
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.services.background import process_graph_projection
from memoryos.db.postgres import get_postgres_conn
from memoryos.core.event_store import log_event
from memoryos.core.cache import stm_cache
from memoryos.models.embeddings import get_embedding_model
from memoryos.core.episodes import process_conversation_log
from memoryos.observability import metrics

class MemoryIngestionService:
    """Centralized service managing validation, classification, SQL/Graph ingestion, and event logging."""
    
    @staticmethod
    async def ingest(data: MemoryIngest, background_tasks: Optional[BackgroundTasks] = None) -> IngestResponse:
        occurred_at = data.occurred_at or datetime.now(timezone.utc)
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        occurred_at = occurred_at.astimezone(timezone.utc)
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
        all_entities = []
        all_relationships = []
        
        llm_extraction_max_events = max(0, int(os.getenv("MEMORYOS_LLM_INGEST_MAX_EVENTS", "3")))
        for event_index, evt in enumerate(events):
            importance = calculate_importance(evt)
            memory_type = classify_memory(evt)
            try:
                emb_res = model.encode(evt)
                embedding = emb_res.tolist() if hasattr(emb_res, "tolist") else list(emb_res)
            except Exception as e:
                logger.error(f"Embedding generation failed: {e}")
                raise HTTPException(status_code=500, detail="Embedding model execution failed.")
            
            # Extract clause-specific graph data once
            # A large pasted note can contain many atomic events. Preserve all
            # of them as memories, but bound optional LLM graph extraction so
            # a single note cannot exhaust a shared daily provider quota.
            clause_graph = extract_entities_and_relationships(evt) if event_index < llm_extraction_max_events else {"entities": [], "relationships": []}
            if not isinstance(clause_graph, dict):
                logger.warning("Entity extractor returned a non-object payload; storing the memory without graph facts.")
                clause_graph = {"entities": [], "relationships": []}
            clause_entities = clause_graph.get("entities", [])
            clause_relationships = clause_graph.get("relationships", [])
            
            all_entities.extend(clause_entities)
            all_relationships.extend(clause_relationships)
            
            clauses_payload.append({
                "content": evt,
                "embedding": embedding,
                "memory_type": memory_type,
                "importance": importance,
                "entities": clause_entities,
                "relationships": clause_relationships,
                "occurred_at": occurred_at.isoformat(),
                "source_event_id": data.source_event_id,
            })
            
        event_payload = {
            "raw_text": data.content,
            "session_id": data.session_id,
            "occurred_at": occurred_at.isoformat(),
            "source_event_id": data.source_event_id,
            "clauses": clauses_payload,
            "entities": all_entities,
            "relationships": all_relationships,
            "metadata": {
                "schema_version": "1.2",
                "embedding_model": "all-MiniLM-L6-v2",
                "extractor_version": "spacy_fallback"
            }
        }
        
        # 2. Persist memory records and their event atomically.
        ingested_memory_ids = []
        conn = None
        try:
            conn = get_postgres_conn()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (data.user_id,))
                if data.session_id:
                    cur.execute(
                        "INSERT INTO sessions (id, user_id, started_at) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                        (data.session_id, data.user_id, occurred_at)
                    )
                
                # Build temporal episodes
                try:
                    process_conversation_log(
                        conn,
                        data.user_id,
                        data.session_id,
                        data.workspace_id,
                        data.content,
                        model,
                        occurred_at=occurred_at,
                        source_event_id=data.source_event_id,
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
                    source_is_new = True
                    
                    cur.execute(
                        "SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s AND fingerprint = %s AND is_active = TRUE",
                        (data.user_id, data.workspace_id, fingerprint)
                    )
                    row = cur.fetchone()
                    if row:
                        existing_memory_id = str(row[0]) if not isinstance(row, dict) else str(row['id'])
                        if data.source_event_id:
                            cur.execute(
                                "SELECT 1 FROM memory_sources WHERE memory_id = %s AND source_event_id = %s",
                                (existing_memory_id, data.source_event_id),
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
                                (occurred_at, occurred_at, existing_memory_id),
                            )
                        frequency_updated = True
                    else:
                        cur.execute(
                            """
                            INSERT INTO memories
                                (id, user_id, session_id, workspace_id, content, embedding, memory_type, importance_score, fingerprint, occurred_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            """,
                            (evt_id, data.user_id, data.session_id, data.workspace_id, evt, embedding, memory_type, importance, fingerprint, occurred_at)
                        )
                    
                    target_memory_id = existing_memory_id if frequency_updated else evt_id
                    if data.source_event_id and source_is_new:
                        cur.execute(
                            """
                            INSERT INTO memory_sources (memory_id, source_event_id, occurred_at)
                            VALUES (%s, %s, %s)
                            ON CONFLICT (memory_id, source_event_id)
                            DO UPDATE SET occurred_at = EXCLUDED.occurred_at
                            """,
                            (target_memory_id, data.source_event_id, occurred_at),
                        )
                    clause_graph = {
                        "entities": item["entities"],
                        "relationships": item["relationships"],
                        "occurred_at": occurred_at.isoformat(),
                        "source_event_id": data.source_event_id,
                    }
                    projection_id = None
                    if source_is_new or not data.source_event_id:
                        projection_id = str(uuid.uuid4())
                        cur.execute(
                            """
                            INSERT INTO graph_projection_outbox
                                (id, memory_id, user_id, workspace_id, content, graph_payload)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (projection_id, target_memory_id, data.user_id, data.workspace_id, evt, json.dumps(clause_graph)),
                        )
                    ingested_memory_ids.append((target_memory_id, evt, embedding, memory_type, frequency_updated, projection_id))

                # The event becomes visible only with the records it describes.
                log_event(data.user_id, data.workspace_id, "MEMORY_INGESTED", event_payload, conn=conn)
            
            conn.commit()
        except HTTPException as he:
            if conn:
                conn.rollback()
            metrics.increment("memoryos_memory_ingestions_total", {"outcome": "rejected"})
            raise he
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Postgres write failed: {e}")
            metrics.increment("memoryos_memory_ingestions_total", {"outcome": "failed"})
            raise HTTPException(status_code=500, detail="Database write error.")
        finally:
            if conn:
                conn.close()
            
        # 3. Graph Ingestion
        process_in_api = os.getenv("MEMORYOS_PROCESS_OUTBOX_INLINE", "false").lower() == "true"
        for target_memory_id, evt, embedding, memory_type, frequency_updated, projection_id in ingested_memory_ids:
            if projection_id and process_in_api and background_tasks:
                background_tasks.add_task(
                    process_graph_projection,
                    projection_id,
                )
            elif projection_id and process_in_api:
                # CLI and replay callers synchronously drain the durable item.
                process_graph_projection(projection_id)
                
            stm_cache.push(data.user_id, data.workspace_id, target_memory_id, evt, embedding)
            
        first_id = ingested_memory_ids[0][0] if ingested_memory_ids else ""
        first_type = ingested_memory_ids[0][3] if ingested_memory_ids else "UNKNOWN"
        is_updated = ingested_memory_ids[0][4] if ingested_memory_ids else False
        
        count = len(events)
        message = (
            f"Memory successfully parsed into {count} atomic events. "
            f"Primary memory ({first_type}) {'updated' if is_updated else 'ingested'} and queued for indexing."
        )
        
        metrics.increment("memoryos_memory_ingestions_total", {"outcome": "success"})
        metrics.increment("memoryos_memory_events_total", amount=count)
        return IngestResponse(
            status="success",
            memory_id=first_id,
            message=message
        )
