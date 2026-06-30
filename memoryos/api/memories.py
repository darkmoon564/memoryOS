import uuid
import math
import json
import hashlib
from datetime import datetime, timezone
from typing import List
from fastapi import APIRouter, Query, HTTPException, BackgroundTasks
from psycopg2.extras import RealDictCursor

# Schemas
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve, IngestResponse, RetrieveResponse, MemoryItem

# Persist & Helpers
from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.models.embeddings import get_embedding_model
from memoryos.models.reranker import get_reranker_model

# Cognitive Core
from memoryos.core.cache import stm_cache
from memoryos.core.classifier import classify_memory
from memoryos.core.scorer import calculate_importance, _execute_decay_logic
from memoryos.services.background import background_graph_ingest

router = APIRouter()

def format_context_markdown(results: list) -> str:
    """Formats retrieval results into structured markdown sections for LLM consumption."""
    sections = {
        "FACTUAL": ("## Known Facts", []),
        "PREFERENCE": ("## Preferences", []),
        "EPISODIC": ("## Recent Events", []),
        "GRAPH_FACT": ("## Knowledge Graph Context", []),
    }
    
    for item in results:
        mem_type = item.get("type", "EPISODIC")
        if mem_type in sections:
            sections[mem_type][1].append(item)
        else:
            sections["EPISODIC"][1].append(item)
    
    parts = []
    for mem_type in ["FACTUAL", "PREFERENCE", "EPISODIC", "GRAPH_FACT"]:
        heading, items = sections[mem_type]
        if items:
            parts.append(heading)
            for item in items:
                parts.append(f"- {item['content']} (confidence: {item['score']:.2f})")
            parts.append("")
    
    return "\n".join(parts).strip() if parts else "No relevant memories found."

def background_access_updates(memory_ids: List[str]):
    """Increment access counts and refresh accessed timestamp."""
    if not memory_ids:
        return
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memories
                SET frequency_count = frequency_count + 1,
                    last_accessed_at = CURRENT_TIMESTAMP
                WHERE id = ANY(%s::uuid[])
                """,
                (memory_ids,)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update access metrics: {e}")

@router.post("/v1/memories", response_model=IngestResponse)
async def ingest_memory(data: MemoryIngest, background_tasks: BackgroundTasks):
    """
    Ingests a memory sentence: computes embeddings, inserts into PostgreSQL, 
    and triggers async background thread for Neo4j entity insertion.
    """
    memory_id = str(uuid.uuid4())
    importance = calculate_importance(data.content)
    memory_type = classify_memory(data.content)
    
    # Generate Embedding locally
    try:
        model = get_embedding_model()
        emb_res = model.encode(data.content)
        embedding = emb_res.tolist() if hasattr(emb_res, "tolist") else list(emb_res)
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail="Embedding model execution failed.")

    # Calculate fingerprint hash for idempotency
    content_clean = data.content.lower().strip()
    raw_fp = f"{data.user_id}:{data.workspace_id}:{content_clean}"
    fingerprint = hashlib.sha256(raw_fp.encode("utf-8")).hexdigest()

    existing_memory_id = None
    frequency_updated = False

    # Write to Postgres/SQLite fallback
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (data.user_id,))
            
            if data.session_id:
                cur.execute(
                    "INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                    (data.session_id, data.user_id)
                )
            
            # Idempotency Check: Look for active memory with matching fingerprint
            cur.execute(
                "SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s AND fingerprint = %s AND is_active = TRUE",
                (data.user_id, data.workspace_id, fingerprint)
            )
            row = cur.fetchone()
            if row:
                existing_memory_id = str(row[0]) if not isinstance(row, dict) else str(row['id'])
                # Update existing memory's access metrics
                cur.execute(
                    "UPDATE memories SET frequency_count = frequency_count + 1, last_accessed_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (existing_memory_id,)
                )
                frequency_updated = True
            else:
                # Insert new memory
                cur.execute(
                    """
                    INSERT INTO memories (id, user_id, session_id, workspace_id, content, embedding, memory_type, importance_score, fingerprint)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (memory_id, data.user_id, data.session_id, data.workspace_id, data.content, embedding, memory_type, importance, fingerprint)
                )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Postgres write failed: {e}")
        raise HTTPException(status_code=500, detail="Database write error.")
        
    target_memory_id = existing_memory_id if frequency_updated else memory_id
    
    # Schedule background tasks for Neo4j Knowledge Graph Ingestion
    background_tasks.add_task(
        background_graph_ingest,
        target_memory_id,
        data.content,
        data.user_id,
        data.workspace_id
    )
    
    # Push to STM cache for recency boosting (if it's a new memory, or refresh existing)
    stm_cache.push(data.user_id, data.workspace_id, target_memory_id, data.content, embedding)
    
    return IngestResponse(
        status="success",
        memory_id=target_memory_id,
        message=f"Memory ({memory_type}) successfully {'updated' if frequency_updated else 'ingested'} and queued for indexing."
    )

@router.post("/v1/memories/retrieve")
async def retrieve_context(data: MemoryRetrieve, format: str = Query("json", description="Response format: 'json' or 'markdown'")):
    """
    Executes hybrid retrieval:
    1. Dense cosine search in PostgreSQL (via pgvector HNSW index)
    2. Trigram keyword search in PostgreSQL (via pg_trgm gin index)
    3. Neo4j graph context extraction
    4. Merging candidates using Reciprocal Rank Fusion (RRF)
    5. Local Cross-Encoder reranking
    """
    try:
        model = get_embedding_model()
        query_emb_res = model.encode(data.query)
        query_embedding = query_emb_res.tolist() if hasattr(query_emb_res, "tolist") else list(query_emb_res)
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail="Embedding failed.")

    candidates = {}
    postgres_candidates = []
    
    try:
        conn = get_postgres_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Dense Vector Search
            cur.execute(
                """
                SELECT id, content, memory_type, importance_score, frequency_count, created_at,
                       (1 - (embedding <=> %s::vector)) AS vector_similarity
                FROM memories
                WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE
                ORDER BY embedding <=> %s::vector
                LIMIT 20
                """,
                (query_embedding, data.user_id, data.workspace_id, query_embedding)
            )
            vector_results = cur.fetchall()
            
            # Sparse Keyword Search
            search_query = f"%{data.query}%"
            cur.execute(
                """
                SELECT id, content, memory_type, importance_score, frequency_count, created_at
                FROM memories
                WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE
                  AND content ILIKE %s
                LIMIT 20
                """,
                (data.user_id, data.workspace_id, search_query)
            )
            keyword_results = cur.fetchall()
            
        conn.close()
    except Exception as e:
        logger.error(f"PostgreSQL query execution failed: {e}")
        raise HTTPException(status_code=500, detail="Database lookup failure.")

    # Retrieve Graph context from Neo4j
    graph_statements = []
    neo4j = get_neo4j_conn()
    if neo4j:
        try:
            graph_query = (
                "MATCH (u:User {id: $user_id})-[:KNOWS_ABOUT]->(e:Entity) "
                "WHERE toLower($query) CONTAINS toLower(e.name) "
                "MATCH (e)-[r]->(target:Entity) "
                "WHERE coalesce(r.is_active, true) = true "
                "RETURN e.name AS source, type(r) AS rel, target.name AS target "
                "LIMIT 10"
            )
            graph_results = neo4j.query(graph_query, {"user_id": data.user_id, "query": data.query})
            for record in graph_results:
                stmt = f"Fact: {record['source']} {record['rel'].lower().replace('_', ' ')} {record['target']}."
                graph_statements.append(stmt)
        except Exception as e:
            logger.error(f"Neo4j query failed: {e}")

    # Build RRF candidate lists
    vector_rank = {str(row['id']): idx for idx, row in enumerate(vector_results)}
    keyword_rank = {str(row['id']): idx for idx, row in enumerate(keyword_results)}
    
    all_keys = set(vector_rank.keys()).union(set(keyword_rank.keys()))
    rrf_candidates = []
    
    info_map = {}
    for r in vector_results:
        info_map[str(r['id'])] = r
    for r in keyword_results:
        if str(r['id']) not in info_map:
            info_map[str(r['id'])] = r

    for doc_id in all_keys:
        v_rank = vector_rank.get(doc_id, 1e9)
        k_rank = keyword_rank.get(doc_id, 1e9)
        score = (1.0 / (60.0 + v_rank)) + (1.0 / (60.0 + k_rank))
        rrf_candidates.append((doc_id, score))
        
    rrf_candidates.sort(key=lambda x: x[1], reverse=True)
    top_candidates = rrf_candidates[:15]
    
    # Boost STM-cached items
    stm_items = stm_cache.get(data.user_id, data.workspace_id)
    stm_ids = {item["memory_id"] for item in stm_items}
    
    final_items = []
    if top_candidates:
        pairs = [(data.query, info_map[doc_id]['content']) for doc_id, _ in top_candidates]
        try:
            reranker = get_reranker_model()
            scores_res = reranker.predict(pairs)
            rerank_scores = scores_res.tolist() if hasattr(scores_res, "tolist") else list(scores_res)
            
            for idx, (doc_id, _) in enumerate(top_candidates):
                item_info = info_map[doc_id]
                created_val = item_info['created_at']
                created_str = created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val)
                mem_type = item_info.get('memory_type', 'EPISODIC') or 'EPISODIC'
                score = float(rerank_scores[idx])
                if doc_id in stm_ids:
                    score *= 1.5
                final_items.append({
                    "memory_id": doc_id,
                    "content": item_info['content'],
                    "score": score,
                    "type": mem_type,
                    "created_at": created_str
                })
        except Exception as e:
            logger.error(f"Reranker failed: {e}. Falling back to RRF rankings.")
            for doc_id, score in top_candidates:
                item_info = info_map[doc_id]
                created_val = item_info['created_at']
                created_str = created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val)
                mem_type = item_info.get('memory_type', 'EPISODIC') or 'EPISODIC'
                rrf_score = float(score)
                if doc_id in stm_ids:
                    rrf_score *= 1.5
                final_items.append({
                    "memory_id": doc_id,
                    "content": item_info['content'],
                    "score": rrf_score,
                    "type": mem_type,
                    "created_at": created_str
                })
                
    # Append Graph statements to context results
    for index, stmt in enumerate(graph_statements):
        final_items.append({
            "memory_id": f"graph_{index}_{uuid.uuid4().hex[:8]}",
            "content": stmt,
            "score": 0.85,
            "type": "GRAPH_FACT",
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        
    final_items.sort(key=lambda x: x['score'], reverse=True)
    results = final_items[:data.limit]
    
    # Increment access logs in background
    background_access_updates([item['memory_id'] for item in results if not item['memory_id'].startswith("graph_")])
    
    token_count = sum(len(item['content'].split()) for item in results)
    
    if format == "markdown":
        markdown_text = format_context_markdown(results)
        return {
            "markdown": markdown_text,
            "results": [MemoryItem(**item) for item in results],
            "context_token_count": int(token_count * 1.3)
        }
    
    return RetrieveResponse(
        results=[MemoryItem(**item) for item in results],
        context_token_count=int(token_count * 1.3)
    )

@router.post("/v1/memories/decay")
async def apply_decay():
    """
    Executes scoring decay updates on memories.
    Flag records as inactive when overall selection score is < 0.15.
    """
    try:
        decayed_count = _execute_decay_logic()
    except Exception as e:
        logger.error(f"Failed to run decay cron job: {e}")
        raise HTTPException(status_code=500, detail="Decay process failed.")
        
    return {"status": "success", "archived_count": decayed_count}

@router.post("/v1/memories/consolidate")
async def consolidate_memories(user_id: str = Query(...), workspace_id: str = Query("default")):
    """
    Consolidation engine: merges near-duplicate memories and deduplicates graph entities.
    """
    merged_count = 0
    entity_dedup_count = 0
    
    try:
        conn = get_postgres_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, content, embedding, importance_score, frequency_count "
                "FROM memories WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE",
                (user_id, workspace_id)
            )
            rows = cur.fetchall()
            
            if len(rows) < 2:
                return {"status": "success", "merged_memories": 0, "deduplicated_entities": 0,
                        "message": "Not enough memories to consolidate."}
            
            memories = []
            for row in rows:
                emb = row['embedding']
                if isinstance(emb, str):
                    emb = json.loads(emb)
                memories.append({
                    "id": str(row['id']),
                    "content": row['content'],
                    "embedding": emb,
                    "importance": float(row['importance_score']),
                    "frequency": int(row['frequency_count'])
                })
            
            deactivate_ids = set()
            for i in range(len(memories)):
                if memories[i]["id"] in deactivate_ids:
                    continue
                for j in range(i + 1, len(memories)):
                    if memories[j]["id"] in deactivate_ids:
                        continue
                    a, b = memories[i]["embedding"], memories[j]["embedding"]
                    dot = sum(x * y for x, y in zip(a, b))
                    norm_a = math.sqrt(sum(x * x for x in a))
                    norm_b = math.sqrt(sum(x * x for x in b))
                    sim = dot / (norm_a * norm_b) if norm_a and norm_b else 0.0
                    
                    if sim > 0.88:
                        if memories[i]["importance"] >= memories[j]["importance"]:
                            survivor, victim = memories[i], memories[j]
                        else:
                            survivor, victim = memories[j], memories[i]
                        
                        deactivate_ids.add(victim["id"])
                        cur.execute(
                            "UPDATE memories SET frequency_count = frequency_count + %s WHERE id = %s",
                            (victim["frequency"], survivor["id"])
                        )
                        merged_count += 1
            
            for vid in deactivate_ids:
                cur.execute("UPDATE memories SET is_active = FALSE WHERE id = %s", (vid,))
        
        conn.commit()
        conn.close()
        
    except Exception as e:
        logger.error(f"Consolidation failed: {e}")
        raise HTTPException(status_code=500, detail=f"Consolidation failed: {e}")
    
    neo4j = get_neo4j_conn()
    if neo4j:
        try:
            dupes = neo4j.query(
                "MATCH (e:Entity {workspace_id: $workspace_id}) "
                "WITH toLower(e.name) AS lname, collect(e) AS nodes "
                "WHERE size(nodes) > 1 "
                "RETURN lname, [n IN nodes | n.name] AS names",
                {"workspace_id": workspace_id}
            )
            for dupe_group in dupes:
                names = dupe_group.get("names", [])
                if len(names) > 1:
                    keep = names[0]
                    for remove in names[1:]:
                        neo4j.query(
                            "MATCH (old:Entity {name: $old_name, workspace_id: $ws}) "
                            "MATCH (keep:Entity {name: $keep_name, workspace_id: $ws}) "
                            "OPTIONAL MATCH (old)-[r]->() "
                            "DELETE r, old",
                            {"old_name": remove, "keep_name": keep, "ws": workspace_id}
                        )
                        entity_dedup_count += 1
        except Exception as e:
            logger.error(f"Neo4j entity dedup failed: {e}")
    
    return {
        "status": "success",
        "merged_memories": merged_count,
        "deduplicated_entities": entity_dedup_count,
        "message": f"Consolidated {merged_count} duplicate memories and {entity_dedup_count} duplicate entities."
    }

@router.delete("/v1/memories")
async def clear_all_memories(user_id: str = Query(...)):
    """Transactional purging of user memories in SQL and Neo4j."""
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
        conn.close()
        
        neo4j = get_neo4j_conn()
        if neo4j:
            neo4j.query(
                "MATCH (u:User {id: $user_id}) "
                "DETACH DELETE u",
                {"user_id": user_id}
            )
    except Exception as e:
        logger.error(f"Failed to clear memories: {e}")
        raise HTTPException(status_code=500, detail="Hard deletion failed.")
        
    return {"status": "success", "message": f"All memories for user {user_id} purged successfully."}
