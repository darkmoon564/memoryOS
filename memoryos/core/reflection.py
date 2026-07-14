import os
import time
import json
import threading
from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn
from memoryos.core.episodes import query_llm

def run_reflection(user_id: str, workspace_id: str, neo4j) -> list:
    """
    Reads recent episodes, parses repeated patterns via LLM,
    and updates the Neo4j Knowledge Graph with synthesized long-term knowledge.
    """
    logger.info(f"[Reflection] Starting reflection run for user {user_id} in workspace {workspace_id}")
    
    # 1. Fetch recent episode summaries from DB
    summaries = []
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            # Check if SQLite (tuple) vs Postgres (dict) structure
            cur.execute(
                """
                SELECT summary FROM episodes
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY last_interaction_at DESC
                LIMIT 10
                """,
                (user_id, workspace_id)
            )
            rows = cur.fetchall()
            for r in rows:
                val = r[0] if not isinstance(r, dict) else r["summary"]
                if val:
                    summaries.append(val)
        conn.close()
    except Exception as db_err:
        logger.error(f"[Reflection] Failed to fetch episodes: {db_err}")
        return []
        
    if not summaries:
        logger.info("[Reflection] No episodes found for reflection.")
        return []
        
    transcript = "\n".join([f"- {s}" for s in summaries])
    
    # 2. Extract facts via LLM (or heuristic fallback)
    system_prompt = (
        "You are a cognitive reflection agent. Analyze the following episode summaries of user interactions. "
        "Identify repeated patterns, recurring topics, user preferences, long-term interests, or habits. "
        "Extract generalized semantic knowledge assertions. "
        "Output the result STRICTLY as a JSON list of objects matching this schema: "
        '[{"subject": "user", "predicate": "INTERESTED_IN|USES|WORKS_AT|KNOWS|PREFERS", "object": "concept_name", "confidence": float}]. '
        "Keep entity names lowercase, clean, and singular. Predicate must be one of: INTERESTED_IN, USES, WORKS_AT, KNOWS, PREFERS."
    )
    
    user_prompt = f"Episode summaries:\n{transcript}"
    
    raw_response = query_llm(system_prompt, user_prompt)
    facts = []
    
    if raw_response:
        try:
            # Clean possible markdown block wraps
            clean_res = raw_response.strip()
            if clean_res.startswith("```json"):
                clean_res = clean_res.split("```json")[1].split("```")[0].strip()
            elif clean_res.startswith("```"):
                clean_res = clean_res.split("```")[1].split("```")[0].strip()
            facts = json.loads(clean_res)
        except Exception as e:
            logger.warning(f"[Reflection] Failed to parse LLM response JSON: {e}. Raw response: {raw_response}")
            
    # Fallback heuristic pattern extractor if LLM fails or is unconfigured
    if not facts:
        logger.info("[Reflection] Using fallback heuristic pattern extractor")
        transcript_lower = transcript.lower()
        heuristics = [
            ("python", "uses", "python"),
            ("rust", "uses", "rust"),
            ("neovim", "uses", "neovim"),
            ("docker", "uses", "docker"),
            ("postgresql", "uses", "postgresql"),
            ("seattle", "lives_in", "seattle"),
            ("acme", "works_at", "acme corp.")
        ]
        for trigger, pred, obj in heuristics:
            if trigger in transcript_lower:
                facts.append({
                    "subject": "user",
                    "predicate": pred.upper(),
                    "object": obj,
                    "confidence": 0.85
                })
                
    if not facts:
        logger.info("[Reflection] No semantic facts synthesized.")
        return []
        
    logger.info(f"[Reflection] Synthesized {len(facts)} facts: {facts}")
    
    # 3. Commit synthesized facts to Neo4j
    committed_facts = []
    for fact in facts:
        subj = fact.get("subject", "user").lower().strip()
        pred = fact.get("predicate", "USES").upper().strip()
        obj = fact.get("object", "").lower().strip()
        conf = float(fact.get("confidence", 0.85))
        
        if not obj or pred not in ["INTERESTED_IN", "USES", "WORKS_AT", "KNOWS", "PREFERS", "LIVES_IN"]:
            continue
            
        try:
            # Graph update: Merge subject and object entities, and create relationship
            # Merge source entity
            neo4j.query(
                "MERGE (s:Entity {name: $source, workspace_id: $workspace_id}) SET s.type = 'Person'",
                {"source": subj, "workspace_id": workspace_id}
            )
            # Merge target entity
            neo4j.query(
                "MERGE (t:Entity {name: $target, workspace_id: $workspace_id})",
                {"target": obj, "workspace_id": workspace_id}
            )
            # Merge relationship
            neo4j.query(
                f"MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}}) "
                f"MATCH (t:Entity {{name: $target, workspace_id: $workspace_id}}) "
                f"MERGE (s)-[r:{pred}]->(t) "
                f"SET r.confidence = $confidence, r.is_active = true",
                {
                    "source": subj,
                    "target": obj,
                    "workspace_id": workspace_id,
                    "confidence": conf
                }
            )
            
            # Connect User Node to both entities via KNOWS_ABOUT
            user_query = """
                MERGE (u:User {id: $user_id, workspace_id: $workspace_id})
                MERGE (s:Entity {name: $source, workspace_id: $workspace_id})
                MERGE (t:Entity {name: $target, workspace_id: $workspace_id})
                MERGE (u)-[:KNOWS_ABOUT]->(s)
                MERGE (u)-[:KNOWS_ABOUT]->(t)
            """
            neo4j.query(user_query, {
                "user_id": user_id,
                "workspace_id": workspace_id,
                "source": subj,
                "target": obj
            })
            
            committed_facts.append(fact)
        except Exception as graph_err:
            logger.error(f"[Reflection] Failed to commit fact to graph: {fact} Error: {graph_err}")
            
    # 4. Archive raw conversation logs linked to the workspace to reduce retrieval noise
    if committed_facts:
        try:
            from psycopg2.extras import RealDictCursor
            conn = get_postgres_conn()
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, user_id, session_id, workspace_id, episode_id, content, created_at
                    FROM conversation_logs
                    WHERE user_id = %s AND workspace_id = %s
                    """,
                    (user_id, workspace_id)
                )
                logs_to_archive = cur.fetchall()
                
                if logs_to_archive:
                    archive_dir = "archive"
                    os.makedirs(archive_dir, exist_ok=True)
                    archive_path = os.path.join(archive_dir, f"archived_logs_{user_id}.jsonl")
                    
                    with open(archive_path, "a", encoding="utf-8") as f:
                        for log in logs_to_archive:
                            created_val = log["created_at"]
                            log["created_at"] = created_val.isoformat() if hasattr(created_val, "isoformat") else str(created_val)
                            f.write(json.dumps(log) + "\n")
                            
                    log_ids = [log["id"] for log in logs_to_archive]
                    placeholders = ",".join(["%s"] * len(log_ids))
                    cur.execute(
                        f"DELETE FROM conversation_logs WHERE id IN ({placeholders})",
                        tuple(log_ids)
                    )
            conn.commit()
            conn.close()
            logger.info(f"[Reflection] Archived {len(logs_to_archive)} conversation logs for user {user_id}")
        except Exception as arch_err:
            logger.error(f"[Reflection] Failed to archive conversation logs: {arch_err}")
            
    logger.info(f"[Reflection] Successfully committed {len(committed_facts)} facts to Neo4j")
    return committed_facts

def start_reflection_daemon(neo4j):
    """Starts the offline Reflection Daemon thread."""
    def daemon_loop():
        # Read daemon check interval from env
        interval = int(os.getenv("REFLECTION_INTERVAL_SECONDS", "3600"))
        logger.info(f"[Reflection Daemon] Periodically active every {interval}s")
        while True:
            time.sleep(interval)
            try:
                conn = get_postgres_conn()
                users = []
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT user_id, workspace_id FROM memories")
                    rows = cur.fetchall()
                    for r in rows:
                        users.append((
                            r[0] if not isinstance(r, dict) else r["user_id"],
                            r[1] if not isinstance(r, dict) else r["workspace_id"]
                        ))
                conn.close()
                
                for u_id, w_id in users:
                    run_reflection(u_id, w_id, neo4j)
            except Exception as e:
                logger.error(f"[Reflection Daemon] Loop iteration error: {e}")
                
    thread = threading.Thread(target=daemon_loop, name="ReflectionDaemon", daemon=True)
    thread.start()
