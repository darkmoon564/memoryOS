import os
from neo4j import GraphDatabase
from memoryos.config import logger, _mock_graph_data

class MockNeo4jDriver:
    """Mock Neo4j graph driver storing relationships in memory."""
    def __init__(self):
        self.data = _mock_graph_data
        self.data.setdefault("aliases", {})
        
    def query(self, query_string, parameters=None):
        parameters = parameters or {}
        # print(f"[MOCK_NEO4J] Query: {query_string[:100]} | Params: {parameters}")
        if "MATCH (e:Entity {name: $name" in query_string:
            name = parameters.get("name")
            workspace_id = parameters.get("workspace_id")
            for ent_name, info in self.data["entities"].items():
                if ent_name == name and info.get("workspace") == workspace_id:
                    return [{"name": ent_name}]
            return []
            
        elif "MATCH (a:Alias {name: $name" in query_string:
            name = parameters.get("name")
            workspace_id = parameters.get("workspace_id")
            canonical = self.data["aliases"].get(name)
            if canonical:
                return [{"canonical": canonical}]
            return []
            
        elif "MATCH (e:Entity {workspace_id: $workspace_id}) RETURN e.name" in query_string:
            workspace_id = parameters.get("workspace_id")
            results = []
            for ent_name, info in self.data["entities"].items():
                if info.get("workspace") == workspace_id:
                    results.append({"name": ent_name})
            return results
            
        elif "MERGE (a:Alias" in query_string:
            alias_name = parameters.get("alias_name")
            self.data["aliases"][alias_name] = "temp"
            return []
            
        elif "MERGE (a)-[r:ALIAS_OF]->(e)" in query_string:
            alias_name = parameters.get("alias_name")
            canonical_name = parameters.get("canonical_name")
            self.data["aliases"][alias_name] = canonical_name
            return []

        elif "KNOWS_ABOUT" in query_string and "source" in parameters:
            user_id = parameters.get("user_id", "")
            source = parameters.get("source", "")
            target = parameters.get("target", "")
            workspace_id = parameters.get("workspace_id", "")
            
            exists_s = False
            for rel in self.data["relationships"]:
                if rel["source"] == user_id and rel["target"] == source and rel["type"] == "KNOWS_ABOUT":
                    exists_s = True
                    rel["is_active"] = True
                    break
            if not exists_s:
                self.data["relationships"].append({
                    "source": user_id,
                    "target": source,
                    "type": "KNOWS_ABOUT",
                    "workspace_id": workspace_id,
                    "is_active": True
                })
                
            exists_t = False
            for rel in self.data["relationships"]:
                if rel["source"] == user_id and rel["target"] == target and rel["type"] == "KNOWS_ABOUT":
                    exists_t = True
                    rel["is_active"] = True
                    break
            if not exists_t:
                self.data["relationships"].append({
                    "source": user_id,
                    "target": target,
                    "type": "KNOWS_ABOUT",
                    "workspace_id": workspace_id,
                    "is_active": True
                })

        elif "MERGE (u:User" in query_string:
            u_id = parameters.get("user_id")
            self.data["users"][u_id] = {"workspace": parameters.get("workspace_id")}
            
        elif "MERGE (e:Entity" in query_string or "MERGE (t:Entity" in query_string or "MERGE (p:Entity" in query_string:
            name = parameters.get("name")
            ent_type = parameters.get("type")
            if not ent_type:
                if "t:Entity" in query_string:
                    ent_type = "Topic"
                elif "p:Entity" in query_string:
                    ent_type = "Profile"
                elif "e:Entity" in query_string:
                    ent_type = "Episode"
                else:
                    ent_type = "Entity"
            self.data["entities"][name] = {"type": ent_type, "workspace": parameters.get("workspace_id")}
            
        elif "MERGE (w:Workflow" in query_string:
            name = parameters.get("name")
            self.data["entities"][name] = {"type": "Workflow", "workspace": parameters.get("workspace_id"), "description": parameters.get("description")}
            
        elif "MERGE" in query_string and any(rel_type in query_string for rel_type in ["LEARNING_TOPIC", "BELONGS_TO_TOPIC", "HAS_PROFILE", "BELONGS_TO_PROFILE", "HAS_WORKFLOW", "USES_TECH"]):
            if "BELONGS_TO_TOPIC" in query_string:
                source = parameters.get("ep_name")
                target = parameters.get("topic_name")
            elif "BELONGS_TO_PROFILE" in query_string:
                source = parameters.get("topic_name")
                target = parameters.get("prof_fact")
            elif "HAS_WORKFLOW" in query_string:
                source = parameters.get("user_id")
                target = parameters.get("name")
            elif "USES_TECH" in query_string:
                source = parameters.get("name")
                target = parameters.get("tech")
            else:
                source = parameters.get("user_id")
                target = parameters.get("name")
                
            workspace_id = parameters.get("workspace_id")
            
            rel_type = "LEARNING_TOPIC"
            for r_type in ["LEARNING_TOPIC", "BELONGS_TO_TOPIC", "HAS_PROFILE", "BELONGS_TO_PROFILE", "HAS_WORKFLOW", "USES_TECH"]:
                if r_type in query_string:
                    rel_type = r_type
                    break
                    
            exists = False
            for r in self.data["relationships"]:
                if r["source"] == source and r["target"] == target and r["type"] == rel_type:
                    exists = True
                    r["is_active"] = True
                    break
            if not exists:
                self.data["relationships"].append({
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "workspace_id": workspace_id,
                    "is_active": True
                })
            
        elif "MERGE (s)-[r:" in query_string:
            source = parameters.get("source")
            target = parameters.get("target")
            source_memory_id = parameters.get("source_memory_id")
            timestamp = parameters.get("timestamp")
            confidence = parameters.get("confidence", 0.9)
            workspace_id = parameters.get("workspace_id")
            
            rel_type = "KNOWS_ABOUT"
            for r_type in ["WORKS_AT", "INTERESTED_IN", "USES", "LIVES_IN", "KNOWS"]:
                if r_type in query_string:
                    rel_type = r_type
                    break
            
            # Check if this relationship already exists
            existing = None
            for r in self.data["relationships"]:
                if r["source"] == source and r["target"] == target and r["type"] == rel_type:
                    existing = r
                    break
                    
            if existing:
                # Mimic ON MATCH
                existing["version"] = existing.get("version", 1) + 1
                if existing.get("source_memory_id") != source_memory_id:
                    existing["evidence_count"] = existing.get("evidence_count", 1) + 1
                existing["source_memory_id"] = source_memory_id
                existing["timestamp"] = timestamp
                existing["confidence"] = confidence
                existing["valid_from"] = existing.get("valid_from") or timestamp
                existing["valid_to"] = None
                existing["superseded_by"] = None
                existing["is_active"] = True
            else:
                # Mimic ON CREATE
                self.data["relationships"].append({
                    "source": source,
                    "target": target,
                    "type": rel_type,
                    "source_memory_id": source_memory_id,
                    "timestamp": timestamp,
                    "confidence": confidence,
                    "workspace_id": workspace_id,
                    "version": 1,
                    "evidence_count": 1,
                    "valid_from": timestamp,
                    "valid_to": None,
                    "is_active": True
                })
        
        elif "type(r) = $rel_type" in query_string and "t.name <>" in query_string:
            source = parameters.get("source", "")
            rel_type = parameters.get("rel_type", "")
            new_target = parameters.get("target", "")
            workspace_id = parameters.get("workspace_id", "")
            results = []
            for rel in self.data["relationships"]:
                if (rel["source"] == source and 
                     rel["type"] == rel_type and 
                     rel["target"] != new_target and
                     rel.get("is_active", True)):
                    results.append({
                        "old_target": rel["target"],
                        "rel_type": rel["type"]
                    })
            return results
        
        elif "SET r.is_active = false" in query_string and "old_target" in str(parameters):
            old_target = parameters.get("old_target", "")
            source = parameters.get("source", "")
            new_target = parameters.get("new_target", "")
            timestamp = parameters.get("timestamp", "")
            for rel in self.data["relationships"]:
                if rel["source"] == source and rel["target"] == old_target:
                    rel["is_active"] = False
                    rel["valid_to"] = timestamp
                    rel["superseded_by"] = new_target
                    
        elif "SUPERSEDED_BY" in query_string:
            old_target = parameters.get("old_target", "")
            new_target = parameters.get("new_target", "")
            rel_type = parameters.get("rel_type", "")
            source = parameters.get("source", "")
            workspace_id = parameters.get("workspace_id", "")
            timestamp = parameters.get("timestamp", "")
            
            exists = False
            for rel in self.data["relationships"]:
                if (rel["source"] == old_target and 
                    rel["target"] == new_target and 
                    rel["type"] == "SUPERSEDED_BY" and
                    rel.get("relationship_type") == rel_type and
                    rel.get("subject") == source):
                    exists = True
                    rel["timestamp"] = timestamp
                    break
            if not exists:
                self.data["relationships"].append({
                    "source": old_target,
                    "target": new_target,
                    "type": "SUPERSEDED_BY",
                    "relationship_type": rel_type,
                    "subject": source,
                    "workspace_id": workspace_id,
                    "timestamp": timestamp,
                    "is_active": True
                })
            
        elif "s.type = 'Person'" in query_string and "MERGE (s)-[r:" in query_string:
            source = parameters.get("source", "")
            target = parameters.get("target", "")
            workspace_id = parameters.get("workspace_id", "")
            confidence = parameters.get("confidence", 0.85)
            
            import re
            match = re.search(r"-\[r:(\w+)\]->", query_string)
            pred = match.group(1) if match else "USES"
            
            if source not in self.data["entities"]:
                self.data["entities"][source] = {"workspace": workspace_id, "type": "Person"}
            if target not in self.data["entities"]:
                self.data["entities"][target] = {"workspace": workspace_id, "type": "Technology"}
                
            exists = False
            for rel in self.data["relationships"]:
                if rel["source"] == source and rel["target"] == target and rel["type"] == pred:
                    exists = True
                    rel["confidence"] = confidence
                    rel["is_active"] = True
                    break
            if not exists:
                self.data["relationships"].append({
                    "source": source,
                    "target": target,
                    "type": pred,
                    "confidence": confidence,
                    "workspace_id": workspace_id,
                    "is_active": True
                })
                


        elif "MATCH (u:User {id: $user_id})-[:KNOWS_ABOUT]->(e:Entity)" in query_string:
            query_text = parameters.get("query", "").lower()
            results = []
            matching_entities = set()
            for ent_name in self.data["entities"].keys():
                if ent_name in query_text:
                    matching_entities.add(ent_name)
            for alias_name, canonical_name in self.data.get("aliases", {}).items():
                if alias_name in query_text:
                    matching_entities.add(canonical_name)
            for rel in self.data["relationships"]:
                if rel["source"] in matching_entities and rel.get("is_active", True):
                    results.append({
                        "source": rel["source"],
                        "rel": rel["type"],
                        "target": rel["target"]
                    })
            return results
            
        elif "shortestPath" in query_string:
            entA = parameters.get("entA")
            entB = parameters.get("entB")
            workspace_id = parameters.get("workspace_id", "")
            
            import collections
            queue = collections.deque([[entA]])
            visited = {entA.lower()}
            found_path = None
            
            while queue:
                path = queue.popleft()
                node = path[-1]
                if node.lower() == entB.lower():
                    found_path = path
                    break
                neighbors = []
                for rel in self.data["relationships"]:
                    if rel.get("workspace_id", "default") == workspace_id and rel.get("is_active", True):
                        if rel["source"].lower() == node.lower():
                            neighbors.append((rel["target"], rel))
                        elif rel["target"].lower() == node.lower():
                            neighbors.append((rel["source"], rel))
                for neighbor, rel in neighbors:
                    if neighbor.lower() not in visited:
                        visited.add(neighbor.lower())
                        queue.append(path + [neighbor])
                        
            results = []
            if found_path and len(found_path) <= 5:
                for i in range(len(found_path) - 1):
                    u = found_path[i]
                    v = found_path[i+1]
                    matched_rel = None
                    for rel in self.data["relationships"]:
                        if rel.get("workspace_id", "default") == workspace_id and rel.get("is_active", True):
                            if (rel["source"].lower() == u.lower() and rel["target"].lower() == v.lower()) or \
                               (rel["source"].lower() == v.lower() and rel["target"].lower() == u.lower()):
                                matched_rel = rel
                                break
                    if matched_rel:
                        results.append({
                            "source": matched_rel["source"],
                            "rel": matched_rel["type"],
                            "target": matched_rel["target"]
                        })
            return results

        elif "cluster.name" in query_string or "neighbor.name" in query_string:
            entities_lower = parameters.get("entities_lower", [])
            workspace_id = parameters.get("workspace_id", "")
            
            results = []
            for ent in entities_lower:
                clusters = []
                for rel in self.data["relationships"]:
                    if (rel.get("workspace_id", "default") == workspace_id and
                        rel["source"].lower() == ent.lower() and
                        rel["type"] in ["BELONGS_TO_TOPIC", "BELONGS_TO_PROFILE"] and
                        rel.get("is_active", True)):
                        clusters.append(rel["target"])
                        
                for cluster in clusters:
                    for rel in self.data["relationships"]:
                        if (rel.get("workspace_id", "default") == workspace_id and
                            rel["target"].lower() == cluster.lower() and
                            rel["type"] in ["BELONGS_TO_TOPIC", "BELONGS_TO_PROFILE"] and
                            rel["source"].lower() != ent.lower() and
                            rel.get("is_active", True)):
                            results.append({
                                "source": ent,
                                "cluster": cluster,
                                "neighbor": rel["source"]
                            })
            return results
            
        elif "IN $entities_lower" in query_string:
            entities_lower = parameters.get("entities_lower", [])
            workspace_id = parameters.get("workspace_id", "")
            # print(f"[MOCK_NEO4J] IN_ENTITIES: entities_lower={entities_lower} workspace={workspace_id} rels={self.data['relationships']}")
            
            matching_entities = set(entities_lower)
            for alias_name, canonical_name in self.data.get("aliases", {}).items():
                if alias_name.lower() in entities_lower:
                    matching_entities.add(canonical_name.lower())
                    
            results = []
            
            if "relationships(p)" in query_string or "*1..3" in query_string:
                visited = set()
                queue = list(matching_entities)
                hop_map = {ent: 0 for ent in queue}
                
                while queue:
                    curr = queue.pop(0)
                    curr_hop = hop_map[curr]
                    if curr_hop >= 3:
                        continue
                    if curr in visited:
                        continue
                    visited.add(curr)
                    
                    for rel in self.data["relationships"]:
                        if (rel.get("workspace_id", "default") == workspace_id and 
                            rel["source"].lower() == curr and 
                            rel.get("is_active", True) and
                            rel["type"] != "SUPERSEDED_BY"):
                            
                            results.append({
                                "source": rel["source"],
                                "rel": rel["type"],
                                "target": rel["target"]
                            })
                            target_lower = rel["target"].lower()
                            if target_lower not in hop_map:
                                hop_map[target_lower] = curr_hop + 1
                                queue.append(target_lower)
            else:
                for rel in self.data["relationships"]:
                    if (rel.get("workspace_id", "default") == workspace_id and 
                        rel["source"].lower() in matching_entities and 
                        rel.get("is_active", True) and
                        rel["type"] != "SUPERSEDED_BY"):
                        results.append({
                            "source": rel["source"],
                            "rel": rel["type"],
                            "target": rel["target"]
                        })
            return results
        
        elif "toLower(e.name)" in query_string:
            from collections import defaultdict
            workspace_id = parameters.get("workspace_id", "")
            groups = defaultdict(list)
            for name, info in self.data["entities"].items():
                if info.get("workspace") == workspace_id:
                    groups[name.lower()].append(name)
            results = []
            for lname, names in groups.items():
                if len(names) > 1:
                    results.append({"lname": lname, "names": names})
            return results
        
        elif "DELETE r, old" in query_string and "old_name" in str(parameters):
            old_name = parameters.get("old_name", "")
            if old_name in self.data["entities"]:
                del self.data["entities"][old_name]
            self.data["relationships"] = [
                r for r in self.data["relationships"]
                if r["source"] != old_name and r["target"] != old_name
            ]
            
        elif "DETACH DELETE u" in query_string:
            u_id = parameters.get("user_id")
            if u_id in self.data["users"]:
                del self.data["users"][u_id]
                
        return []

    def close(self):
        pass

class Neo4jConnector:
    """Manages connections and queries to local Neo4j or falls back to Mock Graph database."""
    def __init__(self):
        import memoryos.config as config
        if config._use_neo4j_fallback:
            self._driver = MockNeo4jDriver()
            self.is_mock = True
            return
            
        try:
            uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
            user = os.getenv("NEO4J_USER", "neo4j")
            password = os.getenv("NEO4J_PASSWORD", "local_dev_password")
            self._driver = GraphDatabase.driver(uri, auth=(user, password))
            with self._driver.session() as s:
                s.run("RETURN 1")
            self.is_mock = False
            config._use_neo4j_fallback = False
        except Exception as e:
            if config._use_neo4j_fallback is None:
                logger.warning("Neo4j database connection timed out or failed. Graph features operating in Mock mode.")
            self._driver = MockNeo4jDriver()
            self.is_mock = True
            config._use_neo4j_fallback = True

    def close(self):
        self._driver.close()

    def query(self, query_string, parameters=None):
        if self.is_mock:
            return self._driver.query(query_string, parameters)
        with self._driver.session() as session:
            result = session.run(query_string, parameters or {})
            return [record.data() for record in result]

_neo4j_conn = None

def get_neo4j_conn():
    global _neo4j_conn
    if _neo4j_conn is None:
        try:
            _neo4j_conn = Neo4jConnector()
        except Exception as e:
            logger.error(f"Failed to connect to Neo4j: {e}. Graph features will run in mock mode.")
            _neo4j_conn = None
    return _neo4j_conn
