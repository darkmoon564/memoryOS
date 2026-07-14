from memoryos.config import logger

def expand_entities(neo4j, user_id: str, workspace_id: str, entities: list) -> dict:
    """
    Queries Neo4j utilizing Graph Data Science upgrades:
    1. Multi-hop (1..3 hops) variable length paths
    2. Shortest paths between concept entity pairs
    3. Topic/Profile community neighbor expansion
    """
    if not neo4j or not entities:
        return {"expanded_entities": [], "graph_facts": []}
        
    entities_lower = [e.lower().strip() for e in entities if e.strip()]
    if not entities_lower:
        return {"expanded_entities": [], "graph_facts": []}
        
    expanded_entities = set(entities_lower)
    graph_facts = []
    
    # 1. Multi-hop (1..3) Variable Length paths
    try:
        query_multihop = (
            "MATCH (e:Entity {workspace_id: $workspace_id}) "
            "WHERE toLower(e.name) IN $entities_lower "
            "   OR EXISTS { "
            "     MATCH (a:Alias)-[:ALIAS_OF]->(e) "
            "     WHERE toLower(a.name) IN $entities_lower "
            "   } "
            "MATCH p=(e)-[r:KNOWS_ABOUT|LEARNING_TOPIC|BELONGS_TO_TOPIC|HAS_PROFILE|BELONGS_TO_PROFILE|HAS_WORKFLOW|USES_TECH*1..3]->(target:Entity) "
            "WHERE all(rel IN relationships(p) WHERE coalesce(rel.is_active, true) = true) "
            "UNWIND relationships(p) AS rel "
            "RETURN startNode(rel).name AS source, type(rel) AS rel, endNode(rel).name AS target"
        )
        results = neo4j.query(query_multihop, {
            "workspace_id": workspace_id,
            "entities_lower": entities_lower
        })
        for record in results:
            src = record["source"]
            target = record["target"]
            rel_type = record["rel"]
            expanded_entities.add(src.lower())
            expanded_entities.add(target.lower())
            stmt = f"Fact: {src} {rel_type.lower().replace('_', ' ')} {target}."
            if stmt not in graph_facts:
                graph_facts.append(stmt)
    except Exception as e:
        logger.error(f"[Graph Expander] Multi-hop query failed: {e}")

    # 2. Shortest paths between concept pairs (if multiple entities exist)
    if len(entities_lower) >= 2:
        try:
            for i in range(len(entities_lower)):
                for j in range(i + 1, len(entities_lower)):
                    query_shortest = (
                        "MATCH (a:Entity {workspace_id: $workspace_id}), (b:Entity {workspace_id: $workspace_id}) "
                        "WHERE toLower(a.name) = $entA AND toLower(b.name) = $entB "
                        "MATCH p=shortestPath((a)-[*..4]-(b)) "
                        "UNWIND relationships(p) AS rel "
                        "RETURN startNode(rel).name AS source, type(rel) AS rel, endNode(rel).name AS target"
                    )
                    results = neo4j.query(query_shortest, {
                        "workspace_id": workspace_id,
                        "entA": entities_lower[i],
                        "entB": entities_lower[j]
                    })
                    for record in results:
                        src = record["source"]
                        target = record["target"]
                        rel_type = record["rel"]
                        expanded_entities.add(src.lower())
                        expanded_entities.add(target.lower())
                        stmt = f"Fact: {src} {rel_type.lower().replace('_', ' ')} {target}."
                        if stmt not in graph_facts:
                            graph_facts.append(stmt)
        except Exception as e:
            logger.error(f"[Graph Expander] Shortest path query failed: {e}")

    # 3. Topic/Profile community neighbor expansion
    try:
        query_neighbors = (
            "MATCH (e:Entity {workspace_id: $workspace_id})-[:BELONGS_TO_TOPIC|BELONGS_TO_PROFILE]->(cluster:Entity)<-[:BELONGS_TO_TOPIC|BELONGS_TO_PROFILE]-(neighbor:Entity) "
            "WHERE toLower(e.name) IN $entities_lower "
            "RETURN e.name AS source, cluster.name AS cluster, neighbor.name AS neighbor"
        )
        results = neo4j.query(query_neighbors, {
            "workspace_id": workspace_id,
            "entities_lower": entities_lower
        })
        for record in results:
            src = record["source"]
            cluster = record["cluster"]
            neighbor = record["neighbor"]
            expanded_entities.add(src.lower())
            expanded_entities.add(cluster.lower())
            expanded_entities.add(neighbor.lower())
            stmt = f"Fact: {neighbor} belongs to same cluster topic '{cluster}' as {src}."
            if stmt not in graph_facts:
                graph_facts.append(stmt)
    except Exception as e:
        logger.error(f"[Graph Expander] Community neighbor query failed: {e}")

    logger.info(f"[Graph Expander] GDS Traversal expanded {entities} to {list(expanded_entities)} with {len(graph_facts)} facts")
    return {
        "expanded_entities": sorted(list(expanded_entities)),
        "graph_facts": graph_facts
    }
