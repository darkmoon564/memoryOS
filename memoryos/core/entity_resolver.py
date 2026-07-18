import os
from memoryos.config import logger
from memoryos.db.neo4j import get_neo4j_conn

def edit_distance(s1: str, s2: str) -> int:
    """Calculates Levenshtein edit distance between two strings."""
    if len(s1) > len(s2):
        s1, s2 = s2, s1
    distances = range(len(s1) + 1)
    for i2, c2 in enumerate(s2):
        distances_ = [i2+1]
        for i1, c1 in enumerate(s1):
            if c1 == c2:
                distances_.append(distances[i1])
            else:
                distances_.append(1 + min((distances[i1], distances[i1 + 1], distances_[-1])))
        distances = distances_
    return distances[-1]

def resolve_entity(name: str, workspace_id: str, user_id: str) -> str:
    """
    Resolves an extracted entity name to its canonical form using Neo4j database lookups
    and local string similarity metrics.
    
    1. Exact Match: Checks if the name exists exactly as a canonical Entity or Alias.
    2. Token Subset Match: Resolves names with corporate suffixes (e.g. 'Microsoft Corp' -> 'Microsoft').
    3. Edit Distance Match: Resolves typos or slight variations (similarity >= 80%).
    """
    name_clean = name.lower().strip()
    if not name_clean:
        return name
        
    neo4j = get_neo4j_conn()
    if not neo4j:
        return name_clean
        
    try:
        # 1. Exact Canonical Entity Match
        res = neo4j.query(
            "MATCH (e:Entity {name: $name, workspace_id: $workspace_id, user_id: $user_id}) RETURN e.name AS name",
            {"name": name_clean, "workspace_id": workspace_id, "user_id": user_id}
        )
        if res:
            return res[0]["name"]
            
        # 2. Exact Alias Match
        res = neo4j.query(
            "MATCH (a:Alias {name: $name, workspace_id: $workspace_id, user_id: $user_id})-[:ALIAS_OF]->(e:Entity {workspace_id: $workspace_id, user_id: $user_id}) "
            "RETURN e.name AS canonical",
            {"name": name_clean, "workspace_id": workspace_id, "user_id": user_id}
        )
        if res:
            return res[0]["canonical"]
            
        # 3. Fuzzy & Token Containment Matches
        all_entities = neo4j.query(
            "MATCH (e:Entity {workspace_id: $workspace_id, user_id: $user_id}) RETURN e.name AS name",
            {"workspace_id": workspace_id, "user_id": user_id}
        )
        existing_names = [r["name"] for r in all_entities]
        
        generic_stopwords = {"inc", "corp", "co", "ltd", "corporation", "company", "limited", "the", "products", "software", "api"}
        
        for existing in existing_names:
            t_new = set(name_clean.split())
            t_ext = set(existing.split())
            
            t_new_sig = t_new - generic_stopwords
            t_ext_sig = t_ext - generic_stopwords
            
            # Check if significant tokens are subsets of each other
            if t_new_sig and t_ext_sig:
                if t_new_sig.issubset(t_ext_sig) or t_ext_sig.issubset(t_new_sig):
                    logger.info(f"[EntityResolver] Resolved '{name}' to existing canonical '{existing}' via token subset.")
                    return existing
                    
            # Check Levenshtein distance
            max_len = max(len(name_clean), len(existing))
            if max_len > 0:
                sim = 1.0 - (edit_distance(name_clean, existing) / max_len)
                if sim >= 0.80:
                    logger.info(f"[EntityResolver] Resolved '{name}' to existing canonical '{existing}' via edit distance ({sim:.2f}).")
                    return existing
                    
    except Exception as e:
        logger.error(f"[EntityResolver] Lookup query failed: {e}")
        
    return name_clean
