from datetime import datetime, timezone
from memoryos.config import logger
from memoryos.db.postgres import get_postgres_conn

def resolve_contradictions(user_id: str, workspace_id: str, relationships: list, neo4j):
    """
    Detects and resolves contradictory relationships in the knowledge graph.
    When a new relationship conflicts with an existing one of the same type
    from the same source entity, the older one is deactivated.
    Latest timestamp always wins (per architecture §6.2).
    """
    resolved_count = 0
    for rel in relationships:
        # Only resolve contradictions for single-valued relationships
        if rel["type"] not in ["USES", "WORKS_AT", "LIVES_IN"]:
            continue
        try:
            # Check for existing relationships of the same type from the same source
            existing = neo4j.query(
                "MATCH (s:Entity {name: $source, workspace_id: $workspace_id})"
                "-[r]->(t:Entity) "
                "WHERE type(r) = $rel_type AND t.name <> $target AND coalesce(r.is_active, true) = true "
                "RETURN t.name AS old_target, type(r) AS rel_type",
                {
                    "source": rel["source"],
                    "workspace_id": workspace_id,
                    "rel_type": rel["type"],
                    "target": rel["target"]
                }
            )
            
            if existing:
                for old_rel in existing:
                    logger.info(
                        f"[Contradiction] Resolved: {rel['source']} {rel['type']} "
                        f"{old_rel.get('old_target', '?')} -> {rel['target']} (newer wins)"
                    )
                    # Soft-deactivate old conflicting edge to preserve history and mark it superseded
                    timestamp_str = datetime.now(timezone.utc).isoformat()
                    old_target = old_rel.get("old_target", "")
                    
                    neo4j.query(
                        f"MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}})"
                        f"-[r:{rel['type']}]->(t:Entity {{name: $old_target}}) "
                        f"SET r.is_active = false, r.valid_to = $timestamp, r.superseded_by = $new_target",
                        {
                            "source": rel["source"],
                            "workspace_id": workspace_id,
                            "old_target": old_target,
                            "new_target": rel["target"],
                            "timestamp": timestamp_str
                        }
                    )
                    
                    # Create SUPERSEDED_BY relationship between old and new targets
                    neo4j.query(
                        "MATCH (old_t:Entity {name: $old_target, workspace_id: $workspace_id}) "
                        "MATCH (new_t:Entity {name: $new_target, workspace_id: $workspace_id}) "
                        "MERGE (old_t)-[sr:SUPERSEDED_BY {relationship_type: $rel_type, subject: $source, workspace_id: $workspace_id}]->(new_t) "
                        "SET sr.timestamp = $timestamp",
                        {
                            "old_target": old_target,
                            "new_target": rel["target"],
                            "rel_type": rel["type"],
                            "source": rel["source"],
                            "workspace_id": workspace_id,
                            "timestamp": timestamp_str
                        }
                    )
                    
                    resolved_count += 1
                    
                    # Also deactivate old memories containing the contradicted fact
                    try:
                        conn = get_postgres_conn()
                        old_target = old_rel.get('old_target', '')
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE memories SET is_active = FALSE "
                                "WHERE user_id = %s AND workspace_id = %s AND is_active = TRUE "
                                "AND content ILIKE %s AND content ILIKE %s",
                                (user_id, workspace_id, f"%{rel['source']}%", f"%{old_target}%")
                            )
                        conn.commit()
                        conn.close()
                        # Clear contradicted item from short term memory cache
                        from memoryos.core.cache import stm_cache
                        stm_cache.remove(user_id, workspace_id, content_sub=old_target)
                    except Exception as db_err:
                        logger.error(f"Failed to deactivate contradicted memory: {db_err}")
                        
        except Exception as e:
            logger.error(f"Contradiction resolution error: {e}")
    
    if resolved_count > 0:
        logger.info(f"[Contradiction] Total resolved: {resolved_count}")
    return resolved_count
