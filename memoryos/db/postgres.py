import os
import re
import json
import math
import sqlite3
import psycopg2
from psycopg2.extras import RealDictCursor
from memoryos.config import logger

_sqlite_conn = None

def get_fallback_sqlite_conn():
    global _sqlite_conn
    if _sqlite_conn is None:
        logger.info("Initializing in-memory SQLite fallback database...")
        _sqlite_conn = sqlite3.connect(":memory:", check_same_thread=False)
        _sqlite_conn.row_factory = sqlite3.Row
        
        # Auto-initialize fallback schema from schema.sql
        try:
            schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../schema.sql"))
            if not os.path.exists(schema_path):
                schema_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../schema.sql"))
            
            if os.path.exists(schema_path):
                with open(schema_path, "r") as f:
                    schema_sql = f.read()
                # Run the schema statements through MockCursor to clean and translate PostgreSQL syntax
                mock_cur = MockCursor(_sqlite_conn.cursor())
                for stmt in schema_sql.split(";"):
                    stmt_clean = stmt.strip()
                    if stmt_clean:
                        mock_cur.execute(stmt_clean)
                _sqlite_conn.commit()
                logger.info("SQLite fallback database tables created and initialized successfully.")
            else:
                logger.warning(f"Could not find schema.sql at {schema_path}.")
        except Exception as e:
            logger.error(f"Failed to auto-initialize SQLite fallback schema: {e}")
            
    return _sqlite_conn

class MockCursor:
    def __init__(self, sqlite_cursor, dict_cursor=False):
        self.cur = sqlite_cursor
        self.dict_cursor = dict_cursor
        self.description = None
        self._results = None
        self._index = 0

    def execute(self, query, params=None):
        statements = query.split(";")
        if len(statements) > 2:
            for stmt in statements:
                stmt_clean = stmt.strip()
                if stmt_clean:
                    self.execute(stmt_clean)
            return

        query_lines = []
        for line in query.splitlines():
            line_strip = line.strip()
            if line_strip.startswith("--") or line_strip.startswith("#"):
                continue
            if " -- " in line:
                line = line.split(" -- ")[0]
            query_lines.append(line)
        clean_query = "\n".join(query_lines).strip()

        if not clean_query:
            return

        if clean_query.startswith("CREATE EXTENSION") or clean_query.startswith("CREATE INDEX IF NOT EXISTS"):
            if "idx_memories_embedding" in clean_query or "idx_memories_trgm" in clean_query:
                pass
            else:
                return

        if "DROP TABLE" in clean_query:
            clean_query = clean_query.replace(" CASCADE", "")

        if "CREATE INDEX" in clean_query:
            clean_query = re.sub(r"USING\s+hnsw\s*\(embedding\s+vector_cosine_ops\)", "(id)", clean_query)
            clean_query = re.sub(r"USING\s+gin\s*\(content\s+gin_trgm_ops\)", "(content)", clean_query)

        clean_query = re.sub(r"vector\(\d+\)", "TEXT", clean_query)
        clean_query = clean_query.replace("vector", "TEXT")
        clean_query = clean_query.replace("TIMESTAMP WITH TIME ZONE", "TIMESTAMP")
        clean_query = clean_query.replace("JSONB", "TEXT")
        clean_query = re.sub(r"NUMERIC\(\d+,\s*\d+\)", "REAL", clean_query)

        # Remove PostgreSQL typecasts (e.g. ::uuid[], ::vector, ::text)
        clean_query = re.sub(r"::\w+(\[\])?", "", clean_query)
        clean_query = clean_query.replace("%s", "?")
        clean_query = clean_query.replace("ILIKE", "LIKE")

        # Intercept cosine similarity distance calculations
        if "<=>" in clean_query:
            query_embedding = params[0]
            if isinstance(query_embedding, str):
                query_embedding = json.loads(query_embedding)
            user_id = params[1]
            workspace_id = params[2]
            
            self.cur.execute(
                "SELECT id, content, memory_type, importance_score, frequency_count, created_at, embedding FROM memories WHERE user_id = ? AND workspace_id = ? AND is_active = 1",
                (user_id, workspace_id)
            )
            rows = self.cur.fetchall()
            
            results = []
            for row in rows:
                row_dict = dict(row)
                row_emb = json.loads(row_dict["embedding"])
                
                # Cosine Similarity
                dot_product = sum(a * b for a, b in zip(query_embedding, row_emb))
                norm_a = math.sqrt(sum(a * a for a in query_embedding))
                norm_b = math.sqrt(sum(b * b for b in row_emb))
                similarity = dot_product / (norm_a * norm_b) if norm_a and norm_b else 0.0
                
                row_dict["vector_similarity"] = similarity
                del row_dict["embedding"]
                row_dict["importance_score"] = float(row_dict["importance_score"])
                results.append(row_dict)
                
            results.sort(key=lambda x: x["vector_similarity"], reverse=True)
            self._results = results[:20]
            self._index = 0
            return

        clean_params = []
        if params:
            for p in params:
                if isinstance(p, list):
                    clean_params.append(json.dumps(p))
                elif isinstance(p, tuple):
                    clean_params.append(str(p))
                else:
                    clean_params.append(p)
            params = tuple(clean_params)

        if "ANY(?)" in clean_query:
            try:
                uuids_list = json.loads(clean_params[0]) if isinstance(clean_params[0], str) else clean_params[0]
            except Exception:
                uuids_list = clean_params[0]
                
            if not isinstance(uuids_list, list):
                uuids_list = [uuids_list]
            placeholders = ",".join("?" for _ in uuids_list)
            if "= ANY(?)" in clean_query:
                clean_query = clean_query.replace("= ANY(?)", f"IN ({placeholders})")
            else:
                clean_query = clean_query.replace("ANY(?)", f"({placeholders})")
            params = tuple(uuids_list)

        if "ON CONFLICT" in clean_query:
            clean_query = clean_query.replace("ON CONFLICT (id) DO NOTHING", "")
            clean_query = clean_query.replace("INSERT INTO", "INSERT OR IGNORE INTO")

        try:
            self.cur.execute(clean_query, params or ())
        except Exception as e:
            if "no such table" in str(e) and "DROP TABLE" in clean_query:
                return
            logger.error(f"SQLite query failed: {clean_query} Error: {e}")
            raise e
            
        self._results = None
        self._index = 0

    def fetchall(self):
        if self._results is not None:
            return self._results
            
        rows = self.cur.fetchall()
        if self.dict_cursor:
            col_names = [desc[0] for desc in self.cur.description]
            return [dict(zip(col_names, row)) for row in rows]
        return rows

    def fetchone(self):
        if self._results is not None:
            if self._index < len(self._results):
                res = self._results[self._index]
                self._index += 1
                return res
            return None
            
        row = self.cur.fetchone()
        if row and self.dict_cursor:
            col_names = [desc[0] for desc in self.cur.description]
            return dict(zip(col_names, row))
        return row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass

class MockPostgresConnection:
    def __init__(self):
        self.sqlite_conn = get_fallback_sqlite_conn()
        self.dict_cursor = False

    def cursor(self, cursor_factory=None):
        if cursor_factory == RealDictCursor:
            self.dict_cursor = True
        else:
            self.dict_cursor = False
        return MockCursor(self.sqlite_conn.cursor(), self.dict_cursor)

    def commit(self):
        self.sqlite_conn.commit()

    def rollback(self):
        self.sqlite_conn.rollback()

    def close(self):
        pass

from psycopg2.pool import ThreadedConnectionPool

_postgres_pool = None

def init_postgres_pool():
    global _postgres_pool
    if _postgres_pool is not None:
        return
    import memoryos.config as config
    if config._use_postgres_fallback:
        return
    try:
        _postgres_pool = ThreadedConnectionPool(
            minconn=1,
            maxconn=20,
            host=os.getenv("POSTGRES_HOST", "localhost"),
            database=os.getenv("POSTGRES_DB", "memoryos"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", "local_dev_password"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            connect_timeout=1
        )
        logger.info("[Database] Threaded Postgres Connection Pool initialized successfully.")
    except Exception as e:
        if config.allow_in_memory_fallback():
            logger.warning(f"[Database] Failed to initialize connection pool: {e}. Using explicitly enabled in-memory adapter.")
            config._use_postgres_fallback = True
            return
        raise RuntimeError("PostgreSQL connection pool initialization failed; refusing non-durable fallback.") from e

def close_postgres_pool():
    global _postgres_pool
    if _postgres_pool is not None:
        try:
            _postgres_pool.closeall()
            logger.info("[Database] Threaded Postgres Connection Pool closed successfully.")
        except Exception as e:
            logger.error(f"[Database] Error closing connection pool: {e}")
        _postgres_pool = None

class PooledConnectionWrapper:
    def __init__(self, conn, pool):
        self._conn = conn
        self._pool = pool
        
    def __getattr__(self, name):
        return getattr(self._conn, name)
        
    def close(self):
        if self._pool and self._conn:
            self._pool.putconn(self._conn)
            self._conn = None
            self._pool = None
        elif self._conn:
            self._conn.close()
            self._conn = None
            
    def __enter__(self):
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def get_postgres_conn():
    """Return a durable PostgreSQL connection, or an explicitly enabled test adapter."""
    import memoryos.config as config
    if config._use_postgres_fallback:
        return MockPostgresConnection()
        
    global _postgres_pool
    if _postgres_pool is None:
        init_postgres_pool()
        
    if _postgres_pool is not None:
        try:
            conn = _postgres_pool.getconn()
            return PooledConnectionWrapper(conn, _postgres_pool)
        except Exception as e:
            logger.warning(f"Failed to fetch connection from pool: {e}. Falling back to single-use connection.")
            
    try:
        conn = psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            database=os.getenv("POSTGRES_DB", "memoryos"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", "local_dev_password"),
            port=int(os.getenv("POSTGRES_PORT", 5432)),
            connect_timeout=1
        )
        config._use_postgres_fallback = False
        return conn
    except Exception as e:
        if config.allow_in_memory_fallback():
            if config._use_postgres_fallback is None:
                logger.warning("PostgreSQL connection failed. Using explicitly enabled in-memory adapter.")
            config._use_postgres_fallback = True
            return MockPostgresConnection()
        raise RuntimeError("PostgreSQL is unavailable; refusing non-durable fallback.") from e
