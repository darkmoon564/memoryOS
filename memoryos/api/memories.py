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
from memoryos.security import hash_api_key
from memoryos.db.postgres import get_postgres_conn
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.models.embeddings import get_embedding_model
from memoryos.models.reranker import get_reranker_model

# Cognitive Core
from memoryos.core.cache import stm_cache
from memoryos.core.working_memory import working_memory
from memoryos.core.event_store import log_event, replay_events
from memoryos.core.classifier import classify_memory
from memoryos.core.scorer import (
    calculate_importance,
    calculate_decay_strength,
    calculate_recency_strength,
    _execute_decay_logic,
)
from memoryos.core.event_parser import parse_events
from memoryos.services.background import background_graph_ingest
from memoryos.core.episodes import process_conversation_log
from memoryos.core.retrieval_planner import plan_retrieval
from memoryos.core.graph_expander import expand_entities
from memoryos.core.reflection import run_reflection
from memoryos.core.consolidation import consolidate_hierarchy
from memoryos.core.temporal_parser import parse_temporal_window

from fastapi import Header
from memoryos.services.ingestion import MemoryIngestionService

router = APIRouter()

def verify_workspace_key(workspace_id: str, authorization: Optional[str]):
    """Verifies that the workspace key is authorized for the target workspace."""
    if authorization is not None and not isinstance(authorization, str):
        authorization = None
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Missing Authorization Header. Please provide Bearer <key>."
        )
    key = authorization[7:].strip() if authorization.lower().startswith("bearer ") else authorization.strip()
    if not key:
        raise HTTPException(status_code=401, detail="Missing Bearer API key.")

    conn = get_postgres_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT workspace_id FROM api_keys WHERE key_hash = %s", (hash_api_key(key),))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=403, detail="Invalid API key credentials.")

            db_workspace = row[0] if not isinstance(row, dict) else row["workspace_id"]
            if db_workspace != workspace_id:
                raise HTTPException(status_code=403, detail=f"API key does not have access to workspace '{workspace_id}'.")
    finally:
        conn.close()

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
        timeline_items.sort(key=lambda x: x.get("occurred_at") or x.get("created_at", ""))
        parts.append("## Chronological Timeline")
        for item in timeline_items:
            dt_str = (item.get("occurred_at") or item.get("created_at", ""))[:10]
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
    valid_memory_ids = []
    for memory_id in memory_ids:
        try:
            valid_memory_ids.append(str(uuid.UUID(str(memory_id))))
        except (ValueError, TypeError, AttributeError):
            # Graph and workflow result IDs are intentionally synthetic and
            # must never be sent to PostgreSQL's uuid[] cast.
            continue
    if not valid_memory_ids:
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
                (valid_memory_ids,)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to update access metrics: {e}")

@router.post("/v1/memories", response_model=IngestResponse)
async def ingest_memory(
    data: MemoryIngest,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None)
):
    """
    Ingests a memory sentence: parses it into atomic events, and for each event
    computes embeddings, inserts into PostgreSQL, and triggers async background
    thread for Neo4j entity insertion.
    """
    verify_workspace_key(data.workspace_id, authorization)
    return await MemoryIngestionService.ingest(data, background_tasks)

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

    last_accessed_at = item_info.get("last_accessed_at") or created_at
    if isinstance(last_accessed_at, str):
        try:
            last_accessed_at = datetime.fromisoformat(last_accessed_at.replace("Z", "+00:00"))
        except ValueError:
            last_accessed_at = created_at

    recency = calculate_recency_strength(last_accessed_at)

    mem_type = item_info.get("memory_type", "EPISODIC") or "EPISODIC"
    if importance >= 0.75 or mem_type == "FACTUAL" or source == "graph":
        verification = "verified"
    else:
        verification = "unverified"

    decay = calculate_decay_strength(last_accessed_at, importance, frequency)

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


# Small, explicit relation aliases improve sparse recall for ordinary agent
# questions without treating a keyword match as semantic understanding.  The
# expansion is deliberately bounded and is still followed by dense retrieval
# and reranking.
_SPARSE_TERM_ALIASES = {
    "reside": ("live", "lives", "residence"),
    "resides": ("live", "lives", "residence"),
    "residence": ("live", "lives", "reside"),
    "language": ("speak", "speaks", "languages"),
    "languages": ("speak", "speaks", "language"),
    "speak": ("speaks", "language", "languages"),
    "spouse": ("wife", "husband", "partner"),
    "job": ("works", "worked", "employed"),
    "work": ("works", "worked", "employed"),
    "employer": ("works", "worked", "employed"),
}


def _expand_sparse_terms(terms: list[str]) -> list[str]:
    """Return de-duplicated sparse terms plus a small set of relation aliases."""
    expanded: list[str] = []
    for raw_term in terms:
        term = raw_term.strip().lower()
        if not term:
            continue
        if term not in expanded:
            expanded.append(term)
        for alias in _SPARSE_TERM_ALIASES.get(term, ()):
            if alias not in expanded:
                expanded.append(alias)
    return expanded


def _normalized_rank_scores(ordered_ids: list[str], floor: float = 0.15) -> dict[str, float]:
    """Map an ordered candidate list to stable bounded relevance signals.

    The floor keeps a valid bottom-ranked candidate recoverable when the
    caller asks for more results than the usual top few.
    """
    if not ordered_ids:
        return {}
    floor = min(1.0, max(0.0, float(floor)))
    if len(ordered_ids) == 1:
        return {ordered_ids[0]: 1.0}
    denominator = len(ordered_ids) - 1
    return {
        doc_id: floor + ((1.0 - floor) * (1.0 - (index / denominator)))
        for index, doc_id in enumerate(ordered_ids)
    }


def _rank_reranker_candidates(
    candidates: list[tuple[str, float]],
    rerank_scores: list[float],
) -> list[str]:
    """Return reranker order with deterministic ties and validated model output."""
    if len(rerank_scores) != len(candidates):
        raise ValueError("Reranker returned a score count different from the candidate count")
    normalized_scores = [float(score) for score in rerank_scores]
    if not all(math.isfinite(score) for score in normalized_scores):
        raise ValueError("Reranker returned a non-finite score")
    return [
        doc_id
        for _, _, doc_id in sorted(
            ((index, normalized_scores[index], doc_id) for index, (doc_id, _) in enumerate(candidates)),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _ranked_ids(rows: list[dict]) -> list[str]:
    """Deduplicate a database result set without changing its supplied order."""
    return list(dict.fromkeys(str(row["id"]) for row in rows))


def _apply_freshness_to_relevance(relevance_score: float, freshness: float) -> float:
    """Use freshness as a conservative modifier rather than a relevance replacement."""
    bounded_relevance = min(1.0, max(0.0, float(relevance_score)))
    bounded_freshness = min(1.0, max(0.0, float(freshness)))
    # A query match must remain dominant over access age: an old but exact
    # memory should beat a fresh unrelated one. Freshness can still reorder
    # close candidates and is exposed transparently in the response.
    return bounded_relevance * (0.90 + (0.10 * bounded_freshness))

@router.post("/v1/memories/retrieve")
async def retrieve_context(
    data: MemoryRetrieve,
    format: str = Query("json", description="Response format: 'json' or 'markdown'"),
    authorization: Optional[str] = Header(None)
):
    verify_workspace_key(data.workspace_id, authorization)
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
                "MATCH (u:User {id: $user_id, workspace_id: $workspace_id})-[known:KNOWS_ABOUT {user_id: $user_id}]->(e:Entity {workspace_id: $workspace_id, user_id: $user_id}) "
                "WHERE toLower($query) CONTAINS toLower(e.name) "
                "OR EXISTS { "
                "  MATCH (a:Alias {workspace_id: $workspace_id, user_id: $user_id})-[:ALIAS_OF]->(e) "
                "  WHERE toLower($query) CONTAINS toLower(a.name) "
                "} "
                "MATCH (e)-[r]->(target:Entity {workspace_id: $workspace_id, user_id: $user_id}) "
                "WHERE r.user_id = $user_id AND coalesce(r.is_active, true) = true "
                "RETURN e.name AS source, type(r) AS rel, target.name AS target "
                "LIMIT 10"
            )
            graph_results = neo4j.query(
                graph_query,
                {"user_id": data.user_id, "workspace_id": data.workspace_id, "query": data.query},
            )
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
    # A superseded fact is hidden from ordinary current-state retrieval but is
    # still historical evidence when the caller explicitly asks for a time.
    memory_visibility_clause = "TRUE" if start_time and end_time else "is_active = TRUE"

    vector_results = []
    keyword_results = []
    episode_results = []

    try:
        conn = get_postgres_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 3. Dense Vector Search (memories)
            cur.execute(
                f"""
                SELECT id, content, memory_type, importance_score, frequency_count, created_at, occurred_at, last_accessed_at,
                       (1 - (embedding <=> %s::vector)) AS vector_similarity
                FROM memories
                WHERE user_id = %s AND workspace_id = %s AND {memory_visibility_clause}
                ORDER BY embedding <=> %s::vector, id ASC
                LIMIT 20
                """,
                (query_embedding, data.user_id, data.workspace_id, query_embedding)
            )
            vector_results = cur.fetchall()

            # 4. Dense Vector Search (episodes summary)
            cur.execute(
                """
                SELECT id, summary AS content, 'EPISODIC' AS memory_type, 0.80 AS importance_score, 1 AS frequency_count, created_at, created_at AS occurred_at, created_at AS last_accessed_at,
                       (1 - (embedding <=> %s::vector)) AS vector_similarity
                FROM episodes
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY embedding <=> %s::vector, id ASC
                LIMIT 5
                """,
                (query_embedding, data.user_id, data.workspace_id, query_embedding)
            )
            episode_results = cur.fetchall()

            # 5. Scoped Sparse Keyword Search
            sparse_terms = _expand_sparse_terms([
                term.strip()
                for term in [data.query, *keywords[:5], *expanded_entities[:5]]
                if term and term.strip()
            ])
            like_clauses = ["content ILIKE %s" for _ in sparse_terms]
            term_patterns = [f"%{term}%" for term in sparse_terms]
            sparse_match_score = " + ".join(
                "CASE WHEN content ILIKE %s THEN 1 ELSE 0 END" for _ in sparse_terms
            )
            params = [*term_patterns, data.user_id, data.workspace_id, *term_patterns]

            clause_str = " OR ".join(like_clauses)
            cur.execute(
                f"""
                SELECT id, content, memory_type, importance_score, frequency_count, created_at, occurred_at, last_accessed_at,
                       ({sparse_match_score}) AS sparse_match_score
                FROM memories
                WHERE user_id = %s AND workspace_id = %s AND {memory_visibility_clause}
                  AND ({clause_str})
                ORDER BY sparse_match_score DESC, occurred_at DESC, id ASC
                LIMIT 20
                """,
                tuple(params)
            )
            keyword_results = cur.fetchall()

            # If temporal query range is active, also pull all matches in that time range
            if start_time and end_time:
                cur.execute(
                    """
                    SELECT id, content, memory_type, importance_score, frequency_count, created_at, occurred_at, last_accessed_at, 1.0 AS vector_similarity
                    FROM memories
                    WHERE user_id = %s AND workspace_id = %s
                      AND occurred_at BETWEEN %s AND %s
                    ORDER BY occurred_at DESC, id ASC
                    LIMIT 20
                    """,
                    (data.user_id, data.workspace_id, start_time, end_time)
                )
                temporal_mems = cur.fetchall()
                vector_results.extend(temporal_mems)
                keyword_results.extend(temporal_mems)

                cur.execute(
                    """
                    SELECT id, summary AS content, 'EPISODIC' AS memory_type, 0.80 AS importance_score, 1 AS frequency_count, created_at, created_at AS occurred_at, created_at AS last_accessed_at, 1.0 AS vector_similarity
                    FROM episodes
                    WHERE user_id = %s AND workspace_id = %s
                      AND created_at BETWEEN %s AND %s
                    ORDER BY created_at DESC, id ASC
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

        vector_results = [r for r in vector_results if in_range(r.get('occurred_at', r['created_at']))]
        keyword_results = [r for r in keyword_results if in_range(r.get('occurred_at', r['created_at']))]
        episode_results = [r for r in episode_results if in_range(r.get('occurred_at', r['created_at']))]

    # 6. Reciprocal Rank Fusion (RRF)
    vector_rank = {doc_id: idx for idx, doc_id in enumerate(_ranked_ids(vector_results))}
    keyword_rank = {doc_id: idx for idx, doc_id in enumerate(_ranked_ids(keyword_results))}
    episode_rank = {doc_id: idx for idx, doc_id in enumerate(_ranked_ids(episode_results))}

    all_keys = list(dict.fromkeys([*vector_rank, *keyword_rank, *episode_rank]))

    info_map = {}
    for r in vector_results:
        info_map[str(r['id'])] = r
    for r in keyword_results:
        doc_id = str(r['id'])
        if doc_id not in info_map:
            info_map[doc_id] = r
        else:
            # Candidate generation keeps the best dense metadata, but lexical
            # evidence is an independent relevance signal and must not be
            # discarded merely because that memory also appeared in vector
            # search.
            info_map[doc_id]["sparse_match_score"] = max(
                float(info_map[doc_id].get("sparse_match_score", 0) or 0),
                float(r.get("sparse_match_score", 0) or 0),
            )
    for r in episode_results:
        if str(r['id']) not in info_map:
            info_map[str(r['id'])] = {
                "id": r["id"],
                "content": f"[Episode Summary] {r['content']}",
                "memory_type": "EPISODIC",
                "importance_score": r["importance_score"],
                "frequency_count": r["frequency_count"],
                "created_at": r["created_at"],
                "occurred_at": r.get("occurred_at", r["created_at"]),
                "last_accessed_at": r.get("last_accessed_at", r["created_at"])
            }

    # Provenance is loaded separately so vector, sparse, and episode queries
    # stay focused on candidate generation. Episode IDs simply produce no rows.
    if all_keys:
        source_ids_by_memory = {}
        try:
            conn = get_postgres_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT memory_id, source_event_id
                    FROM memory_sources
                    WHERE memory_id = ANY(%s::uuid[])
                    ORDER BY occurred_at ASC, source_event_id ASC
                    """,
                    (all_keys,),
                )
                for row in cur.fetchall():
                    memory_id = str(row["memory_id"])
                    source_ids_by_memory.setdefault(memory_id, []).append(row["source_event_id"])
            conn.close()
        except Exception:
            logger.exception("Could not load memory provenance for retrieval results")
        for memory_id, source_event_ids in source_ids_by_memory.items():
            if memory_id in info_map:
                info_map[memory_id]["source_event_ids"] = source_event_ids

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

        # RRF alone can tie a lexical exact match with an unrelated dense
        # candidate (especially with a small or newly changed embedding
        # model).  Preserve sparse term coverage as a bounded, transparent
        # signal so an exact query match survives candidate truncation and is
        # then judged by the reranker.
        sparse_matches = float(info_map[doc_id].get("sparse_match_score", 0) or 0)
        if sparse_matches:
            score += 0.03 * min(sparse_matches, 3.0)

        rrf_candidates.append((doc_id, score))

    rrf_candidates.sort(key=lambda x: x[1], reverse=True)
    top_candidates = rrf_candidates[:15]

    # Boost STM-cached items
    stm_items = stm_cache.get(data.user_id, data.workspace_id)
    stm_ids = {item["memory_id"] for item in stm_items}

    final_items = []
    rrf_rank_scores = _normalized_rank_scores([doc_id for doc_id, _ in top_candidates])

    def append_memory_item(doc_id: str, relevance_score: float) -> None:
        item_info = info_map[doc_id]
        created_val = item_info['created_at']
        created_str = created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val)
        occurred_val = item_info.get("occurred_at") or created_val
        occurred_str = occurred_val.isoformat() if hasattr(occurred_val, "isoformat") else str(occurred_val)
        mem_type = item_info.get('memory_type', 'EPISODIC') or 'EPISODIC'
        freshness = calculate_decay_strength(
            item_info.get("last_accessed_at") or created_val,
            item_info.get("importance_score", 0.50),
            item_info.get("frequency_count", 1),
        )
        score = _apply_freshness_to_relevance(relevance_score, freshness)
        if doc_id in stm_ids:
            score *= 1.5
        score = min(1.0, apply_goal_boost(item_info['content'], mem_type, score, goal_category))
        m_scores = compile_multidimensional_scores(item_info, score, "user")
        final_items.append({
            "memory_id": doc_id,
            "content": item_info['content'],
            "score": score,
            "type": mem_type,
            "created_at": created_str,
            "occurred_at": occurred_str,
            "source_event_ids": item_info.get("source_event_ids", []),
            **m_scores
        })

    if top_candidates:
        pairs = [(data.query, info_map[doc_id]['content']) for doc_id, _ in top_candidates]
        try:
            reranker = get_reranker_model()
            scores_res = reranker.predict(pairs)
            rerank_scores = scores_res.tolist() if hasattr(scores_res, "tolist") else list(scores_res)
            reranked_ids = _rank_reranker_candidates(top_candidates, rerank_scores)
            rerank_rank_scores = _normalized_rank_scores(reranked_ids)

            for doc_id, _ in top_candidates:
                blended_relevance = (0.75 * rerank_rank_scores[doc_id]) + (0.25 * rrf_rank_scores[doc_id])
                append_memory_item(doc_id, blended_relevance)
        except Exception as e:
            logger.error(f"Reranker failed: {e}. Falling back to RRF rankings.")
            for doc_id, _ in top_candidates:
                append_memory_item(doc_id, rrf_rank_scores[doc_id])

    # Graph statements enrich the returned context but do not compete with
    # calibrated memory relevance through an arbitrary fixed score.
    graph_items = []
    for index, stmt in enumerate(graph_statements):
        graph_item_info = {
            "importance_score": 0.85,
            "frequency_count": 2,
            "created_at": datetime.now(timezone.utc),
            "memory_type": "FACTUAL"
        }
        m_scores = compile_multidimensional_scores(graph_item_info, 0.85, "graph")
        graph_items.append({
            "memory_id": f"graph_{index}_{uuid.uuid4().hex[:8]}",
            "content": stmt,
            "score": 0.85,
            "type": "GRAPH_FACT",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            **m_scores
        })

    final_items.sort(key=lambda x: x['score'], reverse=True)
    results = (final_items + graph_items)[:data.limit]

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
                    steps = row['steps']
                    if isinstance(steps, str):
                        try:
                            parsed_steps = json.loads(steps)
                            steps = parsed_steps if isinstance(parsed_steps, list) else [parsed_steps]
                        except (TypeError, ValueError):
                            steps = [steps]
                    else:
                        steps = list(steps or [])
                    procedural_items.append({
                        "memory_id": f"workflow_{row['id']}",
                        "content": json.dumps(steps, ensure_ascii=False),
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
async def apply_decay(
    workspace_id: str = Query("default"),
    authorization: Optional[str] = Header(None),
):
    """
    Evaluates active memories for retrieval-time decay.
    Ordinary long-term memories are never auto-deactivated by age alone.
    """
    verify_workspace_key(workspace_id, authorization)
    log_event("system", workspace_id, "MEMORY_DECAYED", {})
    try:
        evaluated_count = _execute_decay_logic(workspace_id)
    except Exception as e:
        logger.error(f"Failed to run decay cron job: {e}")
        raise HTTPException(status_code=500, detail="Decay process failed.")

    return {"status": "success", "evaluated_count": evaluated_count, "archived_count": 0}

@router.post("/v1/memories/consolidate")
async def consolidate_memories(
    user_id: str = Query(...),
    workspace_id: str = Query("default"),
    authorization: Optional[str] = Header(None)
):
    verify_workspace_key(workspace_id, authorization)
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
                "MATCH (e:Entity {workspace_id: $workspace_id, user_id: $user_id}) "
                "WITH toLower(e.name) AS lname, collect(e) AS nodes "
                "WHERE size(nodes) > 1 "
                "RETURN lname, [n IN nodes | n.name] AS names",
                {"workspace_id": workspace_id, "user_id": user_id}
            )
            for dupe_group in dupes:
                names = dupe_group.get("names", [])
                if len(names) > 1:
                    keep = names[0]
                    for remove in names[1:]:
                        neo4j.query(
                            "MATCH (old:Entity {name: $old_name, workspace_id: $ws, user_id: $user_id}) "
                            "MATCH (keep:Entity {name: $keep_name, workspace_id: $ws, user_id: $user_id}) "
                            "OPTIONAL MATCH (old)-[r]->() "
                            "DELETE r, old",
                            {"old_name": remove, "keep_name": keep, "ws": workspace_id, "user_id": user_id}
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
async def clear_all_memories(
    user_id: str = Query(...),
    workspace_id: str = Query("default"),
    authorization: Optional[str] = Header(None)
):
    """Purge one user's durable records and queue idempotent graph erasure."""
    verify_workspace_key(workspace_id, authorization)
    try:
        conn = get_postgres_conn()
        deletion_id = str(uuid.uuid4())
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM memories WHERE user_id = %s AND workspace_id = %s",
                (user_id, workspace_id),
            )
            memory_ids = [str(row["id"]) for row in cur.fetchall()]
            cur.execute(
                """
                INSERT INTO graph_deletion_outbox (id, user_id, workspace_id, memory_ids)
                VALUES (%s, %s, %s, %s)
                """,
                (deletion_id, user_id, workspace_id, json.dumps(memory_ids)),
            )
            cur.execute("DELETE FROM memories WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM episodes WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM workflows WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM conversation_logs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM event_store WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM background_jobs WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
            cur.execute("DELETE FROM dead_letter_queue WHERE user_id = %s AND workspace_id = %s", (user_id, workspace_id))
        conn.commit()
        conn.close()
        try:
            stm_cache.clear(user_id, workspace_id)
            working_memory.clear_register(user_id, workspace_id)
        except Exception:
            logger.exception("Durable purge committed, but local cache eviction failed")

        try:
            from memoryos.services.background import process_graph_deletion
            graph_cleanup_completed = process_graph_deletion(deletion_id)
        except Exception:
            # The committed outbox row lets a durable worker finish this after
            # a transient Neo4j or worker failure.
            logger.exception("Durable purge committed; graph cleanup remains queued")
            graph_cleanup_completed = False
    except Exception as e:
        logger.error(f"Failed to clear memories: {e}")
        raise HTTPException(status_code=500, detail="Hard deletion failed.")

    return {
        "status": "success" if graph_cleanup_completed else "accepted",
        "graph_cleanup": "completed" if graph_cleanup_completed else "pending_retry",
        "message": f"Memory data for user {user_id} in workspace {workspace_id} was purged.",
    }

@router.post("/v1/memories/reflect")
async def trigger_reflection(data: MemoryReflect, authorization: Optional[str] = Header(None)):
    """
    Triggers the reflection pipeline manually to synthesize
    raw interaction logs into long-term graph knowledge.
    """
    verify_workspace_key(data.workspace_id, authorization)
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

@router.post("/v1/memories/consolidate/hierarchy")
async def trigger_consolidation(data: MemoryReflect, authorization: Optional[str] = Header(None)):
    """
    Triggers the semantic consolidation pipeline manually to cluster
    episodes into topics and profile roles.
    """
    verify_workspace_key(data.workspace_id, authorization)
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
async def ingest_workflow(data: WorkflowIngest, authorization: Optional[str] = Header(None)):
    verify_workspace_key(data.workspace_id, authorization)
    """
    Ingests a structured step-by-step workflow (procedural memory) into the
    relational database and links it to technical entities in the Neo4j Knowledge Graph.
    """
    log_event(data.user_id, data.workspace_id, "WORKFLOW_INGESTED", data.dict())

    from memoryos.core.event_store import execute_workflow_ingest
    try:
        workflow_id = execute_workflow_ingest(
            user_id=data.user_id,
            workspace_id=data.workspace_id,
            name=data.name,
            description=data.description,
            steps=data.steps
        )
    except Exception as e:
        logger.error(f"[Procedural] Ingestion failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to store workflow in database.")

    return WorkflowResponse(
        status="success",
        workflow_id=workflow_id,
        message=f"Workflow '{data.name}' ingested successfully with {len(data.steps)} steps."
    )

@router.get("/v1/memories/working", response_model=WorkingMemoryResponse)
async def get_working_memory(
    user_id: str,
    workspace_id: str = "default",
    authorization: Optional[str] = Header(None)
):
    """Returns the structured active Working Memory register for a user/workspace."""
    verify_workspace_key(workspace_id, authorization)
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
async def update_working_memory(
    data: WorkingMemoryUpdate,
    authorization: Optional[str] = Header(None)
):
    """Updates selected registers in the structured Working Memory cache."""
    verify_workspace_key(data.workspace_id, authorization)
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
async def trigger_replay(
    data: MemoryReflect,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None)
):
    """Asynchronously starts a state replay job reconstructing Postgres/Neo4j from pre-computed logs."""
    verify_workspace_key(data.workspace_id, authorization)
    from memoryos.core.event_store import create_job, replay_events
    job_id = str(uuid.uuid4())
    try:
        create_job(job_id, "REPLAY", data.user_id, data.workspace_id)
        background_tasks.add_task(replay_events, job_id, data.user_id, data.workspace_id)
    except Exception as e:
        logger.error(f"[Replay] Failed to queue job: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to queue replay: {e}")
    return {"status": "queued", "job_id": job_id, "message": "Asynchronous state replay job queued."}

@router.post("/v1/memories/rebuild")
async def trigger_rebuild(
    data: MemoryReflect,
    background_tasks: BackgroundTasks,
    authorization: Optional[str] = Header(None)
):
    """Asynchronously starts a state rebuild job re-extracting and re-embedding all interaction text logs."""
    verify_workspace_key(data.workspace_id, authorization)
    from memoryos.core.event_store import create_job, rebuild_events
    job_id = str(uuid.uuid4())
    try:
        create_job(job_id, "REBUILD", data.user_id, data.workspace_id)
        background_tasks.add_task(rebuild_events, job_id, data.user_id, data.workspace_id)
    except Exception as e:
        logger.error(f"[Rebuild] Failed to queue job: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to queue rebuild: {e}")
    return {"status": "queued", "job_id": job_id, "message": "Asynchronous state rebuild job queued."}

@router.get("/v1/memories/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    authorization: Optional[str] = Header(None)
):
    """Retrieves status and event progress stats of a background replay/rebuild job."""
    conn = get_postgres_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, job_type, user_id, workspace_id, status, total_events, processed_events, error_message, created_at, updated_at
                FROM background_jobs WHERE id = %s
                """,
                (job_id,)
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Job not found.")

            verify_workspace_key(row["workspace_id"], authorization)
            return dict(row)
    finally:
        conn.close()
