import os
import logging
import json
import time
from dotenv import load_dotenv
from memoryos.observability import request_id

# Load env variables
load_dotenv()

def allow_in_memory_fallback() -> bool:
    """Return whether explicitly non-durable adapters may be used.

    This is intentionally opt-in. A connection failure must not make a
    long-term-memory service acknowledge writes that vanish on restart.
    """
    return os.getenv("MEMORYOS_ALLOW_IN_MEMORY_FALLBACK", "false").lower() == "true"


class RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id.get()
        return True


class JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record):
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Logger configuration. JSON is easy to ingest; set MEMORYOS_LOG_FORMAT=text
# for local development.
handler = logging.StreamHandler()
handler.addFilter(RequestContextFilter())
handler.setFormatter(JsonFormatter() if os.getenv("MEMORYOS_LOG_FORMAT", "json") == "json" else logging.Formatter("%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger("MemoryOS")

# Fallback caches (to avoid repeated connection timeouts in mock environments)
_use_postgres_fallback = None
_use_neo4j_fallback = None

# Global lazy loaded objects
_embedding_model = None
_reranker_model = None
_sqlite_conn = None
_mock_graph_data = {"users": {}, "entities": {}, "relationships": []}
