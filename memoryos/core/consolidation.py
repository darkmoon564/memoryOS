import os
import json
from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn
from memoryos.core.episodes import query_llm

def consolidate_hierarchy(user_id: str, workspace_id: str, neo4j) -> dict:
    """
    Consolidates raw episodic experiences into structured topics and profile nodes
    building a 3-level semantic hierarchy in the Neo4j Knowledge Graph.
    """
    logger.info(f"[Consolidation] Starting semantic consolidation for user {user_id} in workspace {workspace_id}")
    
    # 1. Retrieve raw episode summaries from DB
    episodes = []
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT summary FROM episodes
                WHERE user_id = %s AND workspace_id = %s
                ORDER BY last_interaction_at DESC
                LIMIT 15
                """,
                (user_id, workspace_id)
            )
            rows = cur.fetchall()
            for r in rows:
                val = r[0] if not isinstance(r, dict) else r["summary"]
                if val:
                    episodes.append(val)
        conn.close()
    except Exception as db_err:
        logger.error(f"[Consolidation] Failed to fetch episodes: {db_err}")
        return {"status": "error", "message": "Failed to fetch episodes."}
        
    if not episodes:
        logger.info("[Consolidation] No episodes found for consolidation.")
        return {"status": "success", "message": "No episodes to consolidate.", "topics_count": 0, "profile_count": 0}
        
    transcript = "\n".join([f"- {ep}" for ep in episodes])
    
    # 2. Extract hierarchy via LLM
    system_prompt = (
        "You are a cognitive memory architect. Your job is to build a structured 3-level semantic hierarchy of the user's memories "
        "to consolidate raw episodic experiences into long-term knowledge.\n"
        "Level 1: Episodes (recent user tasks)\n"
        "Level 2: Learning Topics (broader technical skills or subjects)\n"
        "Level 3: Semantic Profile (general developer roles/assertions)\n\n"
        "Given the following raw episodes, generate the hierarchy. Output the result STRICTLY as a JSON object matching this schema:\n"
        '{\n'
        '  "topics": [{"name": "topic_name", "episodes": ["episode_summary_1", ...]}],\n'
        '  "profile": [{"fact": "semantic_profile_fact", "topics": ["topic_name_1", ...]}].\n'
        '}\n'
        "Keep names and facts lowercase, clean, and direct."
    )
    
    user_prompt = f"Episode summaries:\n{transcript}"
    
    raw_response = query_llm(system_prompt, user_prompt)
    hierarchy = {}
    
    if raw_response:
        try:
            clean_res = raw_response.strip()
            if clean_res.startswith("```json"):
                clean_res = clean_res.split("```json")[1].split("```")[0].strip()
            elif clean_res.startswith("```"):
                clean_res = clean_res.split("```")[1].split("```")[0].strip()
            hierarchy = json.loads(clean_res)
        except Exception as e:
            logger.warning(f"[Consolidation] Failed to parse LLM response JSON: {e}. Raw response: {raw_response}")
            
    # Fallback keyword heuristic builder if LLM fails or is unconfigured
    if not hierarchy or "topics" not in hierarchy:
        logger.info("[Consolidation] Using fallback keyword heuristic builder")
        topics = []
        profile = []
        
        rust_episodes = [ep for ep in episodes if "rust" in ep.lower()]
        python_episodes = [ep for ep in episodes if "python" in ep.lower()]
        docker_episodes = [ep for ep in episodes if "docker" in ep.lower() or "container" in ep.lower()]
        
        if rust_episodes:
            topics.append({"name": "rust programming", "episodes": rust_episodes})
            profile.append({"fact": "rust software engineer", "topics": ["rust programming"]})
        if python_episodes:
            topics.append({"name": "python programming", "episodes": python_episodes})
            profile.append({"fact": "python software engineer", "topics": ["python programming"]})
        if docker_episodes:
            topics.append({"name": "container virtualization", "episodes": docker_episodes})
            profile.append({"fact": "devops specialist", "topics": ["container virtualization"]})
            
        if not topics:
            topics.append({"name": "general programming", "episodes": episodes})
            profile.append({"fact": "software developer", "topics": ["general programming"]})
            
        hierarchy = {"topics": topics, "profile": profile}
        
    logger.info(f"[Consolidation] Generated hierarchy structure: {hierarchy}")
    
    # 3. Commit hierarchy to Neo4j Graph
    committed_topics = 0
    committed_profiles = 0
    
    try:
        # Create Topic Nodes and connect to User & Episodes
        for topic in hierarchy.get("topics", []):
            topic_name = topic.get("name", "").lower().strip()
            if not topic_name:
                continue
                
            # Merge Topic Entity
            neo4j.query(
                """
                MERGE (t:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                SET t.type = 'Topic'
                """,
                {"name": topic_name, "workspace_id": workspace_id, "user_id": user_id}
            )
            
            # Connect User to Topic via LEARNING_TOPIC
            neo4j.query(
                """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (t:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                MERGE (u)-[r:LEARNING_TOPIC {user_id: $user_id}]->(t)
                SET r.workspace_id = $workspace_id, r.is_active = true
                """,
                {"user_id": user_id, "name": topic_name, "workspace_id": workspace_id}
            )
            
            # Connect Episodes to Topic
            for ep_text in topic.get("episodes", []):
                ep_clean = ep_text.strip()
                if not ep_clean:
                    continue
                    
                # Merge Episode Entity
                neo4j.query(
                    """
                    MERGE (e:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                    SET e.type = 'Episode'
                    """,
                    {"name": ep_clean, "workspace_id": workspace_id, "user_id": user_id}
                )
                
                # Connect User knows about Episode
                neo4j.query(
                    """
                    MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                    MATCH (e:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                    MERGE (u)-[r:KNOWS_ABOUT {user_id: $user_id}]->(e)
                    SET r.workspace_id = $workspace_id, r.is_active = true
                    """,
                    {"user_id": user_id, "name": ep_clean, "workspace_id": workspace_id}
                )
                
                # Link Episode to Topic
                neo4j.query(
                    """
                    MATCH (e:Entity {name: $ep_name, workspace_id: $workspace_id, user_id: $user_id})
                    MATCH (t:Entity {name: $topic_name, workspace_id: $workspace_id, user_id: $user_id})
                    MERGE (e)-[r:BELONGS_TO_TOPIC {user_id: $user_id}]->(t)
                    SET r.workspace_id = $workspace_id, r.is_active = true
                    """,
                    {"ep_name": ep_clean, "topic_name": topic_name, "workspace_id": workspace_id, "user_id": user_id}
                )
                
            committed_topics += 1
            
        # Create Profile Nodes and connect to User & Topics
        for prof in hierarchy.get("profile", []):
            prof_fact = prof.get("fact", "").lower().strip()
            if not prof_fact:
                continue
                
            # Merge Profile Entity
            neo4j.query(
                """
                MERGE (p:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                SET p.type = 'Profile'
                """,
                {"name": prof_fact, "workspace_id": workspace_id, "user_id": user_id}
            )
            
            # Connect User to Profile via HAS_PROFILE
            neo4j.query(
                """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (p:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id})
                MERGE (u)-[r:HAS_PROFILE {user_id: $user_id}]->(p)
                SET r.workspace_id = $workspace_id, r.is_active = true
                """,
                {"user_id": user_id, "name": prof_fact, "workspace_id": workspace_id}
            )
            
            # Link Topics to Profile parent
            for topic_name in prof.get("topics", []):
                t_clean = topic_name.lower().strip()
                if not t_clean:
                    continue
                    
                neo4j.query(
                    """
                    MATCH (t:Entity {name: $topic_name, workspace_id: $workspace_id, user_id: $user_id})
                    MATCH (p:Entity {name: $prof_fact, workspace_id: $workspace_id, user_id: $user_id})
                    MERGE (t)-[r:BELONGS_TO_PROFILE {user_id: $user_id}]->(p)
                    SET r.workspace_id = $workspace_id, r.is_active = true
                    """,
                    {"topic_name": t_clean, "prof_fact": prof_fact, "workspace_id": workspace_id, "user_id": user_id}
                )
                
            committed_profiles += 1
            
    except Exception as graph_err:
        logger.error(f"[Consolidation] Failed to commit consolidation hierarchy: {graph_err}")
        return {"status": "error", "message": "Failed to update graph hierarchy."}
        
    logger.info(f"[Consolidation] Successfully consolidated {committed_topics} topics and {committed_profiles} profiles.")
    return {
        "status": "success",
        "message": f"Consolidated {committed_topics} topics and {committed_profiles} profiles.",
        "topics_count": committed_topics,
        "profile_count": committed_profiles,
        "hierarchy": hierarchy
    }
