import uuid
import math
import json
import hashlib
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Query, HTTPException, BackgroundTasks
from psycopg2.extras import RealDictCursor

# Schemas
from memoryos.schemas.memory import MemoryIngest, MemoryRetrieve, IngestResponse, RetrieveResponse, MemoryItem, MemoryReflect, WorkflowIngest, WorkflowResponse, WorkingMemoryUpdate, WorkingMemoryResponse

# Persist & Helpers
from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.models.embeddings import get_embedding_model
from memoryos.models.reranker import get_reranker_model

# Cognitive Core
from memoryos.core.cache import stm_cache
from memoryos.core.working_memory import working_memory
from memoryos.core.event_store import log_event, replay_events
from memoryos.core.classifier import classify_memory
from memoryos.core.scorer import calculate_importance, _execute_decay_logic
from memoryos.core.event_parser import parse_events
from memoryos.services.background import background_graph_ingest
from memoryos.core.episodes import process_conversation_log
from memoryos.core.retrieval_planner import plan_retrieval
from memoryos.core.graph_expander import expand_entities
from memoryos.core.reflection import run_reflection
from memoryos.core.consolidation import consolidate_hierarchy
from memoryos.core.temporal_parser import parse_temporal_window

router = APIRouter()

def format_context_markdown(results: list, temporal_range: tuple = None, working_memory_state: dict = None) -> str:
    """Formats retrieval results into structured markdown sections for LLM consumption."""
    sections = {
        "FACTUAL": ("## Known Facts", []),
        "PREFERENCE": ("## Preferences", []),
        "EPISODIC": ("## Recent Events", []),
        "GRAPH_FACT": ("## Knowledge Graph Context", []),
        "PROCEDURAL": ("## Procedural Recipes", []),
    }
    
    timeline_items = []
    procedural_recipes = []
    
    for item in results:
        mem_type = item.get("type", "EPISODIC")
        if mem_type == "PROCEDURAL":
            procedural_recipes.append(item)
            continue
            
        if temporal_range and mem_type != "GRAPH_FACT":
            timeline_items.append(item)
            
        if mem_type in sections:
            sections[mem_type][1].append(item)
        else:
            sections["EPISODIC"][1].append(item)
            
    parts = []
    
    # Prepend Active Working Memory state at the absolute top
    if working_memory_state:
        goal = working_memory_state.get("current_goal")
        constraints = working_memory_state.get("constraints", [])
        plan = working_memory_state.get("current_plan", [])
        scratchpad = working_memory_state.get("scratchpad", "")
        
        if goal or constraints or plan or scratchpad:
            parts.append("## Active Working Memory")
            if goal:
                parts.append(f"- **Goal**: {goal}")
            if constraints:
                parts.append(f"- **Constraints**: {', '.join(constraints)}")
            if plan:
                parts.append("- **Plan**:")
                for idx, step in enumerate(plan, 1):
                    parts.append(f"  {idx}. {step}")
            if scratchpad:
                parts.append(f"- **Scratchpad**: {scratchpad}")
            parts.append("")
    
    # Prepend Procedural Recipes at the very top
    if procedural_recipes:
        parts.append("## Procedural Recipes")
        for recipe in procedural_recipes:
            parts.append(f"### Recipe: {recipe.get('name', 'untitled')}")
            if recipe.get("description"):
                parts.append(f"Description: {recipe['description']}")
            steps_data = recipe['content']
            if isinstance(steps_data, str):
                try:
                    steps = json.loads(steps_data)
                except Exception:
                    steps = [steps_data]
            else:
                steps = steps_data
                
            parts.append("Steps:")
            for idx, step in enumerate(steps, 1):
                parts.append(f"{idx}. {step}")
            parts.append("")
            
    # Chronological Timeline Event sequence
    if timeline_items:
        timeline_items.sort(key=lambda x: x.get("created_at", ""))
        parts.append("## Chronological Timeline")
        for item in timeline_items:
            dt_str = item.get("created_at", "")[:10]
            parts.append(f"- [{dt_str}] {item['content']}")
        parts.append("")
        
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
    Ingests a memory sentence: parses it into atomic events, and for each event
    computes embeddings, inserts into PostgreSQL, and triggers async background
    thread for Neo4j entity insertion.
    """
    log_event(data.user_id, data.workspace_id, "MEMORY_INGESTED", data.dict())
    try:
        events = parse_events(data.content)
    except Exception as e:
        logger.error(f"Event parser failed: {e}. Falling back to raw content.")
        events = [data.content]
        
    if not events:
        events = [data.content]
        
    ingested_memory_ids = []
    
    try:
        model = get_embedding_model()
    except Exception as e:
        logger.error(f"Embedding model initialization failed: {e}")
        raise HTTPException(status_code=500, detail="Embedding model execution failed to initialize.")
        
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (data.user_id,))
            
            
            if data.session_id:
                cur.execute(
                    "INSERT INTO sessions (id, user_id) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                    (data.session_id, data.user_id)
                )
            
            # Process raw conversation log into temporal episodes and summarization
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
                
            for evt in events:
                evt_id = str(uuid.uuid4())
                importance = calculate_importance(evt)
                memory_type = classify_memory(evt)
                
                try:
                    emb_res = model.encode(evt)
                    embedding = emb_res.tolist() if hasattr(emb_res, "tolist") else list(emb_res)
                except Exception as e:
                    logger.error(f"Embedding generation failed for event '{evt}': {e}")
                    raise HTTPException(status_code=500, detail="Embedding model execution failed.")

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
        
    for target_memory_id, evt, embedding, memory_type, frequency_updated in ingested_memory_ids:
        background_tasks.add_task(
            background_graph_ingest,
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

def compile_multidimensional_scores(item_info: dict, score: float, source: str) -> dict:
    """Compiles a complete multidimensional scoring dictionary for retrieved memory items."""
    confidence = float(score)
    importance = float(item_info.get("importance_score", 0.50) or 0.50)
    frequency = int(item_info.get("frequency_count", 1) or 1)
    
    created_at = item_info.get("created_at")
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except Exception:
            created_at = datetime.now(timezone.utc)
            
    if created_at:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        delta_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
        recency = max(0.0, 1.0 - (delta_seconds / (30.0 * 86400.0)))
    else:
        recency = 1.0
        
    mem_type = item_info.get("memory_type", "EPISODIC") or "EPISODIC"
    if importance >= 0.75 or mem_type == "FACTUAL" or source == "graph":
        verification = "verified"
    else:
        verification = "unverified"
        
    decay_level = float(item_info.get("decay_level", 0.0) or 0.0)
    decay = max(0.1, 1.0 - decay_level)
    
    return {
        "confidence": confidence,
        "importance": importance,
        "frequency": frequency,
        "recency": recency,
        "verification": verification,
        "source": source,
        "decay": decay
    }

def classify_goal_category(goal: Optional[str]) -> str:
    """Classifies user goal into one of specific domains (DEVELOPER, SHOPPING, RESEARCH, GENERAL)."""
    if not goal:
        return "GENERAL"
    goal_lower = goal.lower()
    
    dev_keywords = ["code", "deploy", "build", "rust", "python", "docker", "api", "database", "repository", "git", "programming", "plugin", "setup", "software", "development"]
    shop_keywords = ["buy", "purchase", "shopping", "budget", "price", "cost", "store", "product", "hire", "usd", "pay", "rate"]
    research_keywords = ["paper", "study", "research", "find information", "summary", "analyze", "trends", "science", "fact"]
    
    if any(kw in goal_lower for kw in dev_keywords):
        return "DEVELOPER"
    if any(kw in goal_lower for kw in shop_keywords):
        return "SHOPPING"
    if any(kw in goal_lower for kw in research_keywords):
        return "RESEARCH"
    return "GENERAL"

def apply_goal_boost(content: str, mem_type: str, score: float, category: str) -> float:
    """Applies domain-specific score boosting to align memory with agent goals."""
    boosted_score = score
    content_lower = content.lower()
    
    if category == "DEVELOPER":
        tech_terms = ["python", "rust", "docker", "neovim", "git", "github", "api", "database", "postgres", "sqlite", "sql", "axum", "plugin", "server", "coding"]
        if any(term in content_lower for term in tech_terms):
            boosted_score *= 1.25
    elif category == "SHOPPING":
        if any(term in content_lower for term in ["$", "usd", "price", "budget", "cost", "fee", "rate", "usd"]) or any(char.isdigit() for char in content):
            boosted_score *= 1.25
    elif category == "RESEARCH":
        if mem_type == "FACTUAL":
            boosted_score *= 1.25
    return boosted_score

@router.post("/v1/memories/retrieve")
async def retrieve_context(data: MemoryRetrieve, format: str = Query("json", description="Response format: 'json' or 'markdown'")):
    """
    Executes hybrid structured retrieval:
    1. Retrieval Planner intent parsing & entity extraction
    2. Entity Graph Expansion via Neo4j
    3. Episode Summary semantic vector search
    4. Guided Dense Vector & Keyword Search scoped/boosted by graph entities
    5. RRF & Cross-Encoder reranking
    """
    # 1. Retrieval Planner intent parsing & entity extraction
    try:
        plan = plan_retrieval(data.query)
        entities = plan.get("entities", [])
        intent = plan.get("intent", "factual_lookup")
        keywords = plan.get("keywords", [])
    except Exception as e:
        logger.error(f"Retrieval Planner failed: {e}")
        entities = []
        intent = "factual_lookup"
        keywords = []

    # 2. Entity Graph Expansion via Neo4j
    graph_statements = []
    expanded_entities = []
    
    neo4j = get_neo4j_conn()
    if neo4j and entities:
        try:
            expansion = expand_entities(neo4j, data.user_id, data.workspace_id, entities)
            expanded_entities = expansion.get("expanded_entities", [])
            graph_statements = expansion.get("graph_facts", [])
        except Exception as e:
            logger.error(f"Graph expansion failed: {e}")
            
    # Fallback to substring matching if no graph statements found yet
    if not graph_statements and neo4j:
        try:
            graph_query = (
                "MATCH (u:User {id: $user_id})-[:KNOWS_ABOUT]->(e:Entity) "
                "WHERE toLower($query) CONTAINS toLower(e.name) "
                "OR EXISTS { "
                "  MATCH (a:Alias)-[:ALIAS_OF]->(e) "
                "  WHERE toLower($query) CONTAINS toLower(a.name) "
                "} "
                "MATCH (e)-[r]->(target:Entity) "
                "WHERE coalesce(r.is_active, true) = true "
                "RETURN e.name AS source, type(r) AS rel, target.name AS target "
                "LIMIT 10"
            )
            graph_results = neo4j.query(graph_query, {"user_id": data.user_id, "query": data.query})
            for record in graph_results:
                stmt = f"Fact: {record['source']} {record['rel'].lower().replace('_', ' ')} {record['target']}."
                graph_statements.append(stmt)
                expanded_entities.append(record['source'].lower())
                expanded_entities.append(record['target'].lower())
        except Exception as e:
            logger.error(f"Neo4j fallback query failed: {e}")

    active_wm = working_memory.get_register(data.user_id, data.workspace_id)
    resolved_goal = data.current_goal or active_wm.get("current_goal")

    goal_category = classify_goal_category(resolved_goal)
    embedding_query = data.query
    if resolved_goal:
        embedding_query = f"{data.query} (Goal: {resolved_goal})"

    # Generate query embedding
    try:
        model = get_embedding_model()
        query_emb_res = model.encode(embedding_query)
        query_embedding = query_emb_res.tolist() if hasattr(query_emb_res, "tolist") else list(query_emb_res)
    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        raise HTTPException(status_code=500, detail="Embedding failed.")

    start_time, end_time = parse_temporal_window(data.query)

    vector_results = []
    keyword_results = []
    episode_results = []

    try:
        conn = get_postgres_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 3. Dense Vector Search (memories)
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
            
            # 4. Dense Vector Search (episodes summary)
            cur.execute(
                """
                SELECT id, summary AS content, 'EPISODIC' AS memory_type, 0.80 AS importance_score, 1 AS frequency_count, created_at,
                       (1 - (embedding <=> %s::vector)) AS vector_similarity
                FROM episodes
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY embedding <=> %s::vector
                LIMIT 5
                """,
                (query_embedding, data.user_id, data.workspace_id, query_embedding)
            )
            episode_results = cur.fetchall()
            
            # 5. Scoped Sparse Keyword Search
            like_clauses = ["content ILIKE %s"]
            params = [data.user_id, data.workspace_id, f"%{data.query}%"]
            
            for ent in expanded_entities[:5]:
                like_clauses.append("content ILIKE %s")
                params.append(f"%{ent}%")
                
            clause_str = " OR ".join(like_clauses)
            cur.execute(
                f"""
                SELECT id, content, memory_type, importance_score, frequency_count, created_at
                FROM memories
                WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE
                  AND ({clause_str})
                LIMIT 20
                """,
                tuple(params)
            )
            keyword_results = cur.fetchall()
            
            # If temporal query range is active, also pull all matches in that time range
            if start_time and end_time:
                cur.execute(
                    """
                    SELECT id, content, memory_type, importance_score, frequency_count, created_at, 1.0 AS vector_similarity
                    FROM memories
                    WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE
                      AND created_at BETWEEN %s AND %s
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (data.user_id, data.workspace_id, start_time, end_time)
                )
                temporal_mems = cur.fetchall()
                vector_results.extend(temporal_mems)
                keyword_results.extend(temporal_mems)
                
                cur.execute(
                    """
                    SELECT id, summary AS content, 'EPISODIC' AS memory_type, 0.80 AS importance_score, 1 AS frequency_count, created_at, 1.0 AS vector_similarity
                    FROM episodes
                    WHERE user_id = %s AND workspace_id = %s
                      AND created_at BETWEEN %s AND %s
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    (data.user_id, data.workspace_id, start_time, end_time)
                )
                temporal_eps = cur.fetchall()
                episode_results.extend(temporal_eps)
                
        conn.close()
    except Exception as e:
        logger.error(f"PostgreSQL query execution failed: {e}")
        raise HTTPException(status_code=500, detail="Database lookup failure.")

    # Filter retrieved results by time range if temporal filter is active
    if start_time and end_time:
        def in_range(created_val):
            if not created_val:
                return False
            if isinstance(created_val, str):
                try:
                    dt = datetime.fromisoformat(created_val.replace("Z", "+00:00"))
                except Exception:
                    return False
            else:
                dt = created_val
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return start_time <= dt <= end_time
            
        vector_results = [r for r in vector_results if in_range(r['created_at'])]
        keyword_results = [r for r in keyword_results if in_range(r['created_at'])]
        episode_results = [r for r in episode_results if in_range(r['created_at'])]

    # 6. Reciprocal Rank Fusion (RRF)
    vector_rank = {str(row['id']): idx for idx, row in enumerate(vector_results)}
    keyword_rank = {str(row['id']): idx for idx, row in enumerate(keyword_results)}
    episode_rank = {str(row['id']): idx for idx, row in enumerate(episode_results)}
    
    all_keys = set(vector_rank.keys()).union(set(keyword_rank.keys())).union(set(episode_rank.keys()))
    
    info_map = {}
    for r in vector_results:
        info_map[str(r['id'])] = r
    for r in keyword_results:
        if str(r['id']) not in info_map:
            info_map[str(r['id'])] = r
    for r in episode_results:
        if str(r['id']) not in info_map:
            info_map[str(r['id'])] = {
                "id": r["id"],
                "content": f"[Episode Summary] {r['content']}",
                "memory_type": "EPISODIC",
                "importance_score": r["importance_score"],
                "frequency_count": r["frequency_count"],
                "created_at": r["created_at"]
            }

    rrf_candidates = []
    for doc_id in all_keys:
        v_rank = vector_rank.get(doc_id, 1e9)
        k_rank = keyword_rank.get(doc_id, 1e9)
        ep_rank = episode_rank.get(doc_id, 1e9)
        
        score = (1.0 / (60.0 + v_rank)) + (1.0 / (60.0 + k_rank)) + (1.0 / (60.0 + ep_rank))
        
        # Entity-based boost
        content_lower = info_map[doc_id]['content'].lower()
        if any(ent in content_lower for ent in expanded_entities):
            score *= 1.2
            
        rrf_candidates.append((doc_id, score))
        
    rrf_candidates.sort(key=lambda x: x[1], reverse=True)
    top_candidates = rrf_candidates[:15]
    
    # Boost STM-cached items
    stm_items = stm_cache.get(data.user_id, data.workspace_id)
    stm_ids = {item["memory_id"] for item in stm_items}
    
    final_items = []
    if top_candidates:
        pairs = [(embedding_query, info_map[doc_id]['content']) for doc_id, _ in top_candidates]
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
                score = apply_goal_boost(item_info['content'], mem_type, score, goal_category)
                m_scores = compile_multidimensional_scores(item_info, score, "user")
                final_items.append({
                    "memory_id": doc_id,
                    "content": item_info['content'],
                    "score": score,
                    "type": mem_type,
                    "created_at": created_str,
                    **m_scores
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
                rrf_score = apply_goal_boost(item_info['content'], mem_type, rrf_score, goal_category)
                m_scores = compile_multidimensional_scores(item_info, rrf_score, "user")
                final_items.append({
                    "memory_id": doc_id,
                    "content": item_info['content'],
                    "score": rrf_score,
                    "type": mem_type,
                    "created_at": created_str,
                    **m_scores
                })
                
    # Append Graph statements to context results
    for index, stmt in enumerate(graph_statements):
        graph_item_info = {
            "importance_score": 0.85,
            "frequency_count": 2,
            "created_at": datetime.now(timezone.utc),
            "memory_type": "FACTUAL"
        }
        m_scores = compile_multidimensional_scores(graph_item_info, 0.85, "graph")
        final_items.append({
            "memory_id": f"graph_{index}_{uuid.uuid4().hex[:8]}",
            "content": stmt,
            "score": 0.85,
            "type": "GRAPH_FACT",
            "created_at": datetime.now(timezone.utc).isoformat(),
            **m_scores
        })
        
    final_items.sort(key=lambda x: x['score'], reverse=True)
    results = final_items[:data.limit]
    
    # 5.5 Check for matching workflows if query/goal suggests procedural intent
    procedural_items = []
    procedural_triggers = ["how to", "how do i", "steps", "workflow", "recipe", "deploy", "build", "configure", "setup"]
    query_lower = data.query.lower()
    goal_lower = data.current_goal.lower() if data.current_goal else ""
    
    if any(trigger in query_lower for trigger in procedural_triggers) or any(trigger in goal_lower for trigger in procedural_triggers):
        try:
            import re
            conn = get_postgres_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                words = [w for w in re.findall(r"\b\w{3,}\b", query_lower + " " + goal_lower)]
                if words:
                    like_clauses = ["name ILIKE %s" for _ in words] + ["description ILIKE %s" for _ in words]
                    params = [f"%{w}%" for w in words] * 2
                    clause_str = " OR ".join(like_clauses)
                    cur.execute(
                        f"""
                        SELECT id, name, description, steps, created_at
                        FROM workflows
                        WHERE user_id = %s AND workspace_id = %s
                          AND ({clause_str})
                        LIMIT 5
                        """,
                        (data.user_id, data.workspace_id, *params)
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, name, description, steps, created_at
                        FROM workflows
                        WHERE user_id = %s AND workspace_id = %s
                        LIMIT 5
                        """,
                        (data.user_id, data.workspace_id)
                    )
                workflow_rows = cur.fetchall()
                
                for row in workflow_rows:
                    wf_item_info = {
                        "importance_score": 0.90,
                        "frequency_count": 1,
                        "created_at": row['created_at'],
                        "memory_type": "FACTUAL"
                    }
                    m_scores = compile_multidimensional_scores(wf_item_info, 0.99, "system")
                    procedural_items.append({
                        "memory_id": f"workflow_{row['id']}",
                        "content": row['steps'],
                        "name": row['name'],
                        "description": row['description'],
                        "score": 0.99,
                        "type": "PROCEDURAL",
                        "created_at": row['created_at'].isoformat() if hasattr(row['created_at'], "isoformat") else str(row['created_at']),
                        **m_scores
                    })
            conn.close()
        except Exception as e:
            logger.error(f"[Procedural] Failed to fetch workflows: {e}")

    if procedural_items:
        results = [r for r in results if not r['memory_id'].startswith("workflow_")]
        results = procedural_items + results
        results = results[:data.limit]
    
    # Increment access logs in background
    background_access_updates([item['memory_id'] for item in results if not item['memory_id'].startswith("graph_")])
    
    token_count = sum(len(item['content'].split()) for item in results)
    
    if format == "markdown":
        markdown_text = format_context_markdown(results, temporal_range=(start_time, end_time) if start_time else None, working_memory_state=active_wm)
        return {
            "markdown": markdown_text,
            "results": [MemoryItem(**item) for item in results],
            "context_token_count": int(token_count * 1.3),
            "goal_category": goal_category
        }
    
    return RetrieveResponse(
        results=[MemoryItem(**item) for item in results],
        context_token_count=int(token_count * 1.3),
        goal_category=goal_category
    )

@router.post("/v1/memories/decay")
async def apply_decay():
    """
    Executes scoring decay updates on memories.
    Flag records as inactive when overall selection score is < 0.15.
    """
    log_event("system", "default", "MEMORY_DECAYED", {})
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

@router.post("/v1/memories/reflect")
async def trigger_reflection(data: MemoryReflect):
    """
    Triggers the reflection pipeline manually to synthesize
    raw interaction logs into long-term graph knowledge.
    """
    neo4j = get_neo4j_conn()
    if not neo4j:
        raise HTTPException(status_code=500, detail="Neo4j connection not available.")
        
    try:
        facts = run_reflection(data.user_id, data.workspace_id, neo4j)
    except Exception as e:
        logger.error(f"Reflection execution failed: {e}")
        raise HTTPException(status_code=500, detail="Reflection failed.")
        
    return {
        "status": "success",
        "synthesized_facts_count": len(facts),
        "facts": facts
    }

@router.post("/v1/memories/consolidate")
async def trigger_consolidation(data: MemoryReflect):
    """
    Triggers the semantic consolidation pipeline manually to cluster
    episodes into topics and profile roles.
    """
    log_event(data.user_id, data.workspace_id, "MEMORIES_CONSOLIDATED", data.dict())
    neo4j = get_neo4j_conn()
    if not neo4j:
        raise HTTPException(status_code=500, detail="Neo4j connection not available.")
        
    try:
        res = consolidate_hierarchy(data.user_id, data.workspace_id, neo4j)
    except Exception as e:
        logger.error(f"Consolidation execution failed: {e}")
        raise HTTPException(status_code=500, detail="Consolidation failed.")
        
    return res

@router.post("/v1/memories/workflows")
async def ingest_workflow(data: WorkflowIngest):
    """
    Ingests a structured step-by-step workflow (procedural memory) into the
    relational database and links it to technical entities in the Neo4j Knowledge Graph.
    """
    log_event(data.user_id, data.workspace_id, "WORKFLOW_INGESTED", data.dict())
    workflow_id = str(uuid.uuid4())
    
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO workflows (id, user_id, workspace_id, name, description, steps)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (workflow_id, data.user_id, data.workspace_id, data.name, data.description, json.dumps(data.steps))
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"[Procedural] Failed to insert workflow into DB: {e}")
        raise HTTPException(status_code=500, detail="Failed to store workflow in database.")

    neo4j = get_neo4j_conn()
    if neo4j:
        try:
            neo4j.query(
                """
                MERGE (w:Workflow {name: $name, workspace_id: $workspace_id})
                SET w.description = $description
                """,
                {"name": data.name, "description": data.description, "workspace_id": data.workspace_id}
            )
            
            neo4j.query(
                """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (w:Workflow {name: $name, workspace_id: $workspace_id})
                MERGE (u)-[r:HAS_WORKFLOW]->(w)
                SET r.is_active = true
                """,
                {"user_id": data.user_id, "name": data.name, "workspace_id": data.workspace_id}
            )
            
            tech_keywords = ["docker", "postgres", "sqlite", "neovim", "python", "rust", "railway", "github", "git"]
            detected_techs = set()
            for step in data.steps:
                step_lower = step.lower()
                for tech in tech_keywords:
                    if tech in step_lower:
                        detected_techs.add(tech)
                        
            for tech in detected_techs:
                neo4j.query(
                    """
                    MATCH (t:Entity {name: $tech, workspace_id: $workspace_id})
                    MATCH (w:Workflow {name: $name, workspace_id: $workspace_id})
                    MERGE (w)-[r:USES_TECH]->(t)
                    SET r.is_active = true
                    """,
                    {"tech": tech, "name": data.name, "workspace_id": data.workspace_id}
                )
                
        except Exception as e:
            logger.error(f"[Procedural] Failed to update Neo4j with workflow nodes: {e}")
            
    return WorkflowResponse(
        status="success",
        workflow_id=workflow_id,
        message=f"Workflow '{data.name}' ingested successfully with {len(data.steps)} steps."
    )

@router.get("/v1/memories/working", response_model=WorkingMemoryResponse)
async def get_working_memory(user_id: str, workspace_id: str = "default"):
    """Returns the structured active Working Memory register for a user/workspace."""
    reg = working_memory.get_register(user_id, workspace_id)
    return WorkingMemoryResponse(
        user_id=user_id,
        workspace_id=workspace_id,
        current_goal=reg.get("current_goal"),
        constraints=reg.get("constraints", []),
        current_plan=reg.get("current_plan", []),
        scratchpad=reg.get("scratchpad", ""),
        retained_facts=reg.get("retained_facts", [])
    )

@router.post("/v1/memories/working", response_model=WorkingMemoryResponse)
async def update_working_memory(data: WorkingMemoryUpdate):
    """Updates selected registers in the structured Working Memory cache."""
    kwargs = {
        "current_goal": data.current_goal,
        "constraints": data.constraints,
        "current_plan": data.current_plan,
        "scratchpad": data.scratchpad,
        "retained_facts": data.retained_facts
    }
    reg = working_memory.update_register(data.user_id, data.workspace_id, **kwargs)
    return WorkingMemoryResponse(
        user_id=data.user_id,
        workspace_id=data.workspace_id,
        current_goal=reg.get("current_goal"),
        constraints=reg.get("constraints", []),
        current_plan=reg.get("current_plan", []),
        scratchpad=reg.get("scratchpad", ""),
        retained_facts=reg.get("retained_facts", [])
    )

@router.post("/v1/memories/replay")
async def trigger_state_replay(data: MemoryReflect):
    """
    Clears all derived state and replays all events from event_store in chronological order.
    """
    try:
        await replay_events(data.user_id, data.workspace_id)
        return {"status": "success", "message": "State replayed and rebuilt successfully from event logs."}
    except Exception as e:
        logger.error(f"[EventStore] Replay trigger failed: {e}")
        raise HTTPException(status_code=500, detail="Replay failed.")
