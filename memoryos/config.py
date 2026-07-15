import os
import logging
from dotenv import load_dotenv

# Load env variables
load_dotenv()



# Logger configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MemoryOS")

# Fallback caches (to avoid repeated connection timeouts in mock environments)
_use_postgres_fallback = None
_use_neo4j_fallback = None

# Global lazy loaded objects
_embedding_model = None
_reranker_model = None
_sqlite_conn = None
_mock_graph_data = {"users": {}, "entities": {}, "relationships": []}
