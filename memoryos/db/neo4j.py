import os
from neo4j import GraphDatabase
from memoryos.config import logger, _mock_graph_data

class MockNeo4jDriver:
    """Mock Neo4j graph driver storing relationships in memory."""
    def __init__(self):
        self.data = _mock_graph_data
        
    def query(self, query_string, parameters=None):
        parameters = parameters or {}
        if "MERGE (u:User" in query_string:
            u_id = parameters.get("user_id")
            self.data["users"][u_id] = {"workspace": parameters.get("workspace_id")}
            
        elif "MERGE (e:Entity" in query_string:
            name = parameters.get("name")
            self.data["entities"][name] = {"type": parameters.get("type"), "workspace": parameters.get("workspace_id")}
            
        elif "MERGE (s)-[r:" in query_string:
            source = parameters.get("source")
            target = parameters.get("target")
            rel_type = "KNOWS_ABOUT"
            for r_type in ["WORKS_AT", "INTERESTED_IN", "USES", "LIVES_IN", "KNOWS"]:
                if r_type in query_string:
                    rel_type = r_type
                    break
            self.data["relationships"].append({
                "source": source,
                "target": target,
                "type": rel_type,
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
            for rel in self.data["relationships"]:
                if rel["source"] == source and rel["target"] == old_target:
                    rel["is_active"] = False
            
        elif "MATCH (u:User {id: $user_id})-[:KNOWS_ABOUT]->(e:Entity)" in query_string:
            query_text = parameters.get("query", "").lower()
            results = []
            matching_entities = []
            for ent_name in self.data["entities"].keys():
                if ent_name in query_text:
                    matching_entities.append(ent_name)
            for rel in self.data["relationships"]:
                if rel["source"] in matching_entities and rel.get("is_active", True):
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
