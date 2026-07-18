import os
import uuid
import requests
import json
from datetime import datetime, timezone, timedelta
from memoryos.config import logger

def query_llm(system_prompt: str, user_prompt: str) -> str | None:
    """Utility to query the configured LLM (OpenAI-compatible or Ollama)."""
    if os.getenv("OFFLINE_MODE", "false").lower() == "true":
        # Deterministic test mode must not make an accidental network call to
        # a developer's Ollama or OpenAI-compatible endpoint.
        return None
    timeout = float(os.getenv("LLM_TIMEOUT", "15.0"))
    
    # ── Mode 1: OpenAI-compatible API ──
    llm_api_base = os.getenv("LLM_API_BASE")
    llm_api_key = os.getenv("LLM_API_KEY")
    llm_model = os.getenv("LLM_MODEL")
    
    if llm_api_base and llm_api_key and llm_model:
        try:
            url = f"{llm_api_base.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {llm_api_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": llm_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.0
            }
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code == 200:
                content = response.json()["choices"][0]["message"]["content"].strip()
                logger.info(f"[LLM] Episode summarization completed via LLM API ({llm_model})")
                return content
            else:
                logger.warning(f"LLM API returned status {response.status_code}: {response.text[:200]}")
        except Exception as e:
            logger.warning(f"LLM API call failed: {e}")
        return None
        
    # ── Mode 2: Ollama (local LLM) ──
    ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
    ollama_model = os.getenv("OLLAMA_MODEL", "llama3.2")
    
    try:
        response = requests.post(
            f"{ollama_url}/api/chat",
            json={
                "model": ollama_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "options": {"temperature": 0.0},
                "stream": False
            },
            timeout=timeout
        )
        if response.status_code == 200:
            content = response.json()["message"]["content"].strip()
            logger.info(f"[Ollama] Episode summarization completed via Ollama ({ollama_model})")
            return content
        else:
            logger.warning(f"Ollama returned status {response.status_code}: {response.text[:200]}")
    except Exception as e:
        logger.warning(f"Ollama call failed: {e}")
    return None


def generate_episode_summary(transcript: str) -> str:
    """Generates a dense summary of the conversation transcript."""
    system_prompt = (
        "You are an expert memory summarization assistant. Compress the following raw chat/interaction logs "
        "into a dense, cohesive summary focusing on key factual assertions, user preferences, and context. "
        "Avoid generic filler phrases like 'The user discussed...'. Write the summary directly in the third person. "
        "Keep the summary under 150 words."
    )
    
    summary = query_llm(system_prompt, transcript)
    if summary:
        return summary
        
    # Fallback heuristic summary
    logger.info("[Episode Summarizer] Falling back to heuristic summary generator")
    lines = [line.strip() for line in transcript.split("\n") if line.strip()]
    if not lines:
        return "Empty episode."
    if len(lines) <= 3:
        return " ".join([l.lstrip("- ") for l in lines])
    return f"Interaction including: {lines[0].lstrip('- ')} And: {lines[-1].lstrip('- ')}"


def process_conversation_log(
    conn,
    user_id: str,
    session_id: str,
    workspace_id: str,
    content: str,
    embedding_model,
    occurred_at: datetime | None = None,
    source_event_id: str | None = None,
) -> str:
    """
    Ingests a raw conversation log, groups it into a temporal Episode,
    summarizes the episode's history, and embeddings the summary.
    Returns the episode_id.
    """
    interaction_time = occurred_at or datetime.now(timezone.utc)
    if interaction_time.tzinfo is None:
        interaction_time = interaction_time.replace(tzinfo=timezone.utc)
    log_id = str(uuid.uuid4())
    
    with conn.cursor() as cur:
        # A stable upstream turn identifier makes retries safe for the raw
        # transcript as well as for canonical memory rows.  Older callers
        # without one keep the historical append-only behavior.
        existing_log = None
        existing_episode_id = None
        if source_event_id:
            cur.execute(
                """
                SELECT id, episode_id
                FROM conversation_logs
                WHERE user_id = %s AND workspace_id = %s AND source_event_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id, workspace_id, source_event_id),
            )
            existing_log = cur.fetchone()
            if existing_log:
                log_id = str(existing_log[0] if not isinstance(existing_log, dict) else existing_log["id"])
                existing_episode_id = existing_log[1] if not isinstance(existing_log, dict) else existing_log["episode_id"]

        # 1. Insert the raw conversation log record unless this source turn
        # was already persisted.  The database index is the concurrent-write
        # safeguard; this lookup preserves a successful retry's episode id.
        if existing_episode_id:
            return str(existing_episode_id)
        if not existing_log:
            cur.execute(
                """
                INSERT INTO conversation_logs
                    (id, user_id, session_id, workspace_id, content, source_event_id, occurred_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (log_id, user_id, session_id, workspace_id, content, source_event_id, interaction_time, interaction_time)
            )
        
        # 2. Check for an active Episode within the time threshold (default 10 minutes)
        gap_limit = int(os.getenv("EPISODE_GAP_SECONDS", "600"))
        threshold = interaction_time - timedelta(seconds=gap_limit)
        upper_bound = interaction_time + timedelta(seconds=gap_limit)
        
        # Check if SQLite (tuple) vs Postgres (dict) structure
        cur.execute(
            """
            SELECT id FROM episodes 
            WHERE user_id = %s AND workspace_id = %s 
            AND (session_id = %s OR (session_id IS NULL AND %s IS NULL))
            AND last_interaction_at >= %s
            AND last_interaction_at <= %s
            ORDER BY last_interaction_at DESC LIMIT 1
            """,
                (user_id, workspace_id, session_id, session_id, threshold, upper_bound)
        )
        row = cur.fetchone()
        
        if row:
            # Active episode found
            episode_id = str(row[0]) if not isinstance(row, dict) else str(row["id"])
            # Update last interaction timestamp
            cur.execute(
                """
                UPDATE episodes
                SET last_interaction_at = CASE
                    WHEN last_interaction_at > %s THEN last_interaction_at
                    ELSE %s
                END
                WHERE id = %s
                """,
                (interaction_time, interaction_time, episode_id)
            )
        else:
            # Create a new episode
            episode_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO episodes (id, user_id, session_id, workspace_id, summary, last_interaction_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (episode_id, user_id, session_id, workspace_id, "New interaction.", interaction_time, interaction_time)
            )
            
        # 3. Link the new conversation log to this episode
        cur.execute(
            "UPDATE conversation_logs SET episode_id = %s WHERE id = %s",
            (episode_id, log_id)
        )
        
        # 4. Fetch all logs linked to this episode to build/rebuild the summary
        cur.execute(
            "SELECT content FROM conversation_logs WHERE episode_id = %s ORDER BY occurred_at ASC, created_at ASC",
            (episode_id,)
        )
        log_rows = cur.fetchall()
        
        contents = []
        for r in log_rows:
            val = r[0] if not isinstance(r, dict) else r["content"]
            contents.append(val)
            
        transcript = "\n".join([f"- {c}" for c in contents])
        
        # 5. Summarize and generate embedding over the summary
        summary = generate_episode_summary(transcript)
        
        try:
            emb_res = embedding_model.encode(summary)
            embedding = emb_res.tolist() if hasattr(emb_res, "tolist") else list(emb_res)
        except Exception as e:
            logger.error(f"Failed to generate embedding for episode summary: {e}")
            # Use a zero vector or basic fallback list if encoding fails
            embedding = [0.0] * 384
            
        # 6. Update episode record with the new summary and embedding
        cur.execute(
            "UPDATE episodes SET summary = %s, embedding = %s WHERE id = %s",
            (summary, embedding, episode_id)
        )
        
        return episode_id
