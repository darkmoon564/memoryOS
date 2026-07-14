from datetime import datetime, timezone
from memoryos.config import logger
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.services.extractor import extract_entities_and_relationships
from memoryos.core.contradiction import resolve_contradictions
from memoryos.core.entity_resolver import resolve_entity

def background_graph_ingest(memory_id: str, content: str, user_id: str, workspace_id: str):
    """Background task to extract entities/relations and update the Neo4j Graph with canonicalization."""
    logger.info(f"Triggering entity extraction for memory: {memory_id}")
    
    graph_data = extract_entities_and_relationships(content)
    
    neo4j = get_neo4j_conn()
    if neo4j:
        entities = graph_data.get("entities", [])
        relationships = graph_data.get("relationships", [])
        
        # 1. Resolve raw entity names to canonical names
        resolved_map = {}
        for entity in entities:
            raw_name = entity["name"]
            resolved_map[raw_name] = resolve_entity(raw_name, workspace_id)
            
        for rel in relationships:
            src = rel["source"]
            tgt = rel["target"]
            if src not in resolved_map:
                resolved_map[src] = resolve_entity(src, workspace_id)
            if tgt not in resolved_map:
                resolved_map[tgt] = resolve_entity(tgt, workspace_id)
                
        # Insert User Node
        user_query = "MERGE (u:User {id: $user_id, workspace_id: $workspace_id})"
        neo4j.query(user_query, {"user_id": user_id, "workspace_id": workspace_id})
        
        # 2. Create Canonical Entities and Alias nodes
        for raw_name, canonical_name in resolved_map.items():
            # Find the type matching raw_name if available, default to 'Entity'
            ent_type = "Entity"
            for ent in entities:
                if ent["name"] == raw_name:
                    ent_type = ent["type"]
                    break
                    
            # Create/Merge the Canonical Entity
            ent_query = """
                MERGE (e:Entity {name: $name, workspace_id: $workspace_id})
                SET e.type = $type
            """
            neo4j.query(ent_query, {
                "name": canonical_name,
                "type": ent_type,
                "workspace_id": workspace_id
            })
            
            # If name is an alias, create Alias node and ALIAS_OF relation
            if raw_name != canonical_name:
                logger.info(f"[Graph Ingest] Creating alias '{raw_name}' -> canonical '{canonical_name}'")
                alias_query = """
                    MERGE (a:Alias {name: $alias_name, workspace_id: $workspace_id})
                """
                neo4j.query(alias_query, {"alias_name": raw_name, "workspace_id": workspace_id})
                
                alias_rel_query = """
                    MATCH (a:Alias {name: $alias_name, workspace_id: $workspace_id})
                    MATCH (e:Entity {name: $canonical_name, workspace_id: $workspace_id})
                    MERGE (a)-[r:ALIAS_OF]->(e)
                """
                neo4j.query(alias_rel_query, {
                    "alias_name": raw_name,
                    "canonical_name": canonical_name,
                    "workspace_id": workspace_id
                })
                
            # Connect User to the canonical entity
            user_ent_query = """
                MATCH (u:User {id: $user_id, workspace_id: $workspace_id})
                MATCH (e:Entity {name: $name, workspace_id: $workspace_id})
                MERGE (u)-[r:KNOWS_ABOUT]->(e)
            """
            neo4j.query(user_ent_query, {
                "user_id": user_id,
                "name": canonical_name,
                "workspace_id": workspace_id
            })
            
        # 3. Resolve relationships to canonical names (filtering self-loops)
        resolved_rels = []
        for rel in relationships:
            src_canonical = resolved_map.get(rel["source"], rel["source"])
            tgt_canonical = resolved_map.get(rel["target"], rel["target"])
            if src_canonical == tgt_canonical:
                continue
            resolved_rels.append({
                "source": src_canonical,
                "target": tgt_canonical,
                "type": rel["type"]
            })
            
        # Resolve contradictions on canonical relationships before insertion
        resolve_contradictions(user_id, workspace_id, resolved_rels, neo4j)
        
        # 4. Create Relationships between Canonical Entities
        timestamp_str = datetime.now(timezone.utc).isoformat()
        for rel in resolved_rels:
            rel_query = f"""
                MATCH (s:Entity {{name: $source, workspace_id: $workspace_id}})
                MATCH (t:Entity {{name: $target, workspace_id: $workspace_id}})
                MERGE (s)-[r:{rel['type']}]->(t)
                ON CREATE SET 
                    r.version = 1,
                    r.evidence_count = 1,
                    r.created_at = $timestamp,
                    r.valid_from = $timestamp,
                    r.valid_to = null,
                    r.source_memory_id = $source_memory_id,
                    r.confidence = $confidence,
                    r.workspace_id = $workspace_id,
                    r.is_active = true
                ON MATCH SET 
                    r.version = coalesce(r.version, 1) + 1,
                    r.evidence_count = coalesce(r.evidence_count, 1) + (CASE WHEN r.source_memory_id <> $source_memory_id THEN 1 ELSE 0 END),
                    r.updated_at = $timestamp,
                    r.valid_from = coalesce(r.valid_from, $timestamp),
                    r.valid_to = null,
                    r.superseded_by = null,
                    r.source_memory_id = $source_memory_id,
                    r.confidence = $confidence,
                    r.is_active = true
            """
            neo4j.query(rel_query, {
                "source": rel["source"],
                "target": rel["target"],
                "workspace_id": workspace_id,
                "source_memory_id": memory_id,
                "timestamp": timestamp_str,
                "confidence": float(rel.get("confidence", 0.9))
            })
            
        logger.info(f"Graph ingestion completed for memory: {memory_id}")
