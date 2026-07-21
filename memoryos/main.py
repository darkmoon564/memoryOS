import threading
import time
import uuid
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

# Routers
from memoryos.api.memories import router as memories_router
from memoryos.api.tools import router as tools_router

# Logic & Utils
from memoryos.config import logger
from memoryos.core.scorer import _execute_decay_logic
from memoryos.core.reflection import start_reflection_daemon
from memoryos.db.neo4j import get_neo4j_conn
from memoryos.models.embeddings import get_embedding_model
from memoryos.models.reranker import get_reranker_model
from memoryos.services.background import drain_graph_work
from memoryos.observability import metrics, request_id
from memoryos.migrations import status as migration_status
import memoryos.config as config

_decay_timer = None
_projection_timer = None


def run_graph_projection_sweep():
    """Recover graph projections that survived a request or process failure."""
    global _projection_timer
    try:
        completed = drain_graph_work()
        if completed:
            logger.info("[Scheduler] Completed %s pending graph projections.", completed)
    except Exception:
        logger.exception("[Scheduler] Graph projection sweep failed")
    finally:
        _projection_timer = threading.Timer(60, run_graph_projection_sweep)
        _projection_timer.daemon = True
        _projection_timer.start()

def run_decay_sweep():
    """Background decay sweep that runs periodically."""
    global _decay_timer, _projection_timer
    try:
        logger.info("[Scheduler] Running automatic decay sweep...")
        evaluated = _execute_decay_logic()
        logger.info(f"[Scheduler] Decay sweep completed. Evaluated {evaluated} active memories.")
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

    if not config.allow_in_memory_fallback():
        pending, changed = migration_status()
        if changed:
            raise RuntimeError(f"Applied migrations were modified: {', '.join(changed)}")
        if pending:
            raise RuntimeError(f"Database migrations are pending: {', '.join(pending)}. Run `python -m memoryos.migrations upgrade`.")

    # Force graph verification before accepting requests. In-memory graph mode is
    # only available when explicitly opted into for tests.
    get_neo4j_conn()

    if os.getenv("OFFLINE_MODE", "false").lower() != "true":
        logger.info("[Startup] Verifying semantic embedding and reranker models...")
        get_embedding_model()
        get_reranker_model()

    if os.getenv("MEMORYOS_EMBEDDED_WORKER", "false").lower() == "true":
        logger.warning("[Startup] Embedded graph worker enabled; use `python -m memoryos.worker` for production.")
        run_graph_projection_sweep()
    
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
    if _projection_timer:
        _projection_timer.cancel()
        logger.info("[Shutdown] Graph projection scheduler stopped.")
        
    logger.info("[Shutdown] Closing Threaded Postgres Connection Pool...")
    close_postgres_pool()
    
    logger.info("[Shutdown] Closing Neo4j Driver...")
    close_neo4j_conn()

app = FastAPI(title="AI Memory Operating System (MemoryOS)", version="1.2.0", lifespan=lifespan)

# The bundled FounderOS demo is served separately during local development.
# Keep this intentionally narrow so the API is not exposed to arbitrary browser
# origins; production deployments should place their own frontend origin here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
)


@app.middleware("http")
async def observe_http_request(request: Request, call_next):
    incoming_id = request.headers.get("X-Request-ID", "")
    correlation_id = incoming_id if 0 < len(incoming_id) <= 128 else uuid.uuid4().hex
    token = request_id.set(correlation_id)
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = correlation_id
        return response
    finally:
        route = request.scope.get("route")
        metric_path = getattr(route, "path", request.url.path)
        metrics.increment("memoryos_http_requests_total", {"method": request.method, "path": metric_path, "status": status_code})
        metrics.observe("memoryos_http_request_duration_seconds", time.perf_counter() - started, {"method": request.method, "path": metric_path})
        request_id.reset(token)

@app.get("/health")
async def health_check():
    """Liveness probe. It deliberately does not depend on external services."""
    return {"status": "ok", "version": "1.2.0"}


@app.get("/readyz")
async def readiness_check():
    """Readiness probe for durable stores required to accept memory writes."""
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
        
    ready = postgres_status == "connected" and neo4j_status == "connected"
    body = {
        "status": "ready" if ready else "not_ready",
        "version": "1.2.0",
        "dependencies": {
            "postgres": postgres_status,
            "neo4j": neo4j_status,
        }
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    return PlainTextResponse(metrics.render_prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")

# Register routes
app.include_router(memories_router)
app.include_router(tools_router)
