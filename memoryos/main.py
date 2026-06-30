import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI

# Routers
from memoryos.api.memories import router as memories_router
from memoryos.api.tools import router as tools_router

# Logic & Utils
from memoryos.config import logger
from memoryos.core.scorer import _execute_decay_logic

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

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler for startup/shutdown."""
    global _decay_timer
    logger.info("[Startup] Starting automatic decay scheduler (24h interval)...")
    _decay_timer = threading.Timer(60, run_decay_sweep)  # First run after 60s
    _decay_timer.daemon = True
    _decay_timer.start()
    yield
    if _decay_timer:
        _decay_timer.cancel()
        logger.info("[Shutdown] Decay scheduler stopped.")

app = FastAPI(title="AI Memory Operating System (MemoryOS)", version="1.2.0", lifespan=lifespan)

# Register routes
app.include_router(memories_router)
app.include_router(tools_router)
