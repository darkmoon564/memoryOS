import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI

# Routers
from memoryos.api.memories import router as memories_router
from memoryos.api.tools import router as tools_router

# Logic & Utils
from memoryos.config import logger
from memoryos.core.scorer import _execute_decay_logic
from memoryos.core.reflection import start_reflection_daemon
from memoryos.db.neo4j import get_neo4j_conn

_decay_timer = None

def run_decay_sweep():
    """Background decay sweep that runs periodically."""
    global _decay_timer
    try:
        logger.info("[Scheduler] Running automatic decay sweep...")
        decayed = _execute_decay_logic()
        logger.info(f"[Scheduler] Decay sweep completed. Archived {decayed} memories.")
    except Exception as e:
        logger.error(f"[Scheduler] Decay sweep failed: {e}")
    finally:
        # Schedule next run in 24 hours
        _decay_timer = threading.Timer(86400, run_decay_sweep)
        _decay_timer.daemon = True
        _decay_timer.start()

from memoryos.db.postgres import init_postgres_pool, close_postgres_pool, get_postgres_conn
from memoryos.db.neo4j import close_neo4j_conn

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for startup/shutdown."""
    global _decay_timer
    logger.info("[Startup] Initializing Threaded Postgres Connection Pool...")
    init_postgres_pool()
    
    logger.info("[Startup] Starting automatic decay scheduler (24h interval)...")
    _decay_timer = threading.Timer(60, run_decay_sweep)  # First run after 60s
    _decay_timer.daemon = True
    _decay_timer.start()
    
    logger.info("[Startup] Starting offline reflection daemon...")
    try:
        neo4j = get_neo4j_conn()
        start_reflection_daemon(neo4j)
    except Exception as e:
        logger.error(f"[Startup] Failed to start reflection daemon: {e}")
        
    yield
    
    if _decay_timer:
        _decay_timer.cancel()
        logger.info("[Shutdown] Decay scheduler stopped.")
        
    logger.info("[Shutdown] Closing Threaded Postgres Connection Pool...")
    close_postgres_pool()
    
    logger.info("[Shutdown] Closing Neo4j Driver...")
    close_neo4j_conn()

app = FastAPI(title="AI Memory Operating System (MemoryOS)", version="1.2.0", lifespan=lifespan)

@app.get("/health")
async def health_check():
    """Detailed health check endpoint reporting status of PostgreSQL, Neo4j, and ML models."""
    postgres_status = "connected"
    try:
        conn = get_postgres_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        conn.close()
    except Exception:
        postgres_status = "disconnected"
        
    neo4j_status = "connected"
    try:
        neo4j = get_neo4j_conn()
        if getattr(neo4j, "is_mock", False):
            neo4j_status = "degraded"
        else:
            neo4j.query("RETURN 1")
    except Exception:
        neo4j_status = "disconnected"
        
    from memoryos.api.memories import get_embedding_model, get_reranker_model
    emb_status = "loaded"
    try:
        get_embedding_model()
    except Exception:
        emb_status = "failed"
        
    reranker_status = "loaded"
    try:
        get_reranker_model()
    except Exception:
        reranker_status = "failed"
        
    overall_status = "healthy"
    if postgres_status == "disconnected" or emb_status == "failed":
        overall_status = "unhealthy"
    elif neo4j_status in ["degraded", "disconnected"] or reranker_status == "failed":
        overall_status = "degraded"
        
    return {
        "status": overall_status,
        "version": "1.2.0",
        "dependencies": {
            "postgres": postgres_status,
            "neo4j": neo4j_status,
            "embedding_model": emb_status,
            "reranker_model": reranker_status
        }
    }

# Register routes
app.include_router(memories_router)
app.include_router(tools_router)
