import os
import hashlib
import math
from memoryos.config import logger

class MockEmbeddingModel:
    """Deterministic, zero-dependency offline embedding model fallback."""
    def encode(self, sentences):
        is_single = isinstance(sentences, str)
        if is_single:
            sentences = [sentences]
            
        results = []
        for text in sentences:
            h = hashlib.sha256(text.encode('utf-8')).digest()
            vector = []
            for i in range(384):
                byte_val = h[i % len(h)]
                val = (byte_val * (i + 13)) % 101
                vector.append(float(val) / 50.0 - 1.0)
            
            norm = math.sqrt(sum(x * x for x in vector))
            if norm > 0:
                vector = [x / norm for x in vector]
            results.append(vector)
            
        return results[0] if is_single else results

def get_embedding_model():
    import memoryos.config as config
    if config._embedding_model is None:
        if os.getenv("OFFLINE_MODE", "false").lower() == "true":
            logger.info("OFFLINE_MODE is enabled. Operating in Zero-Dependency Mock ML mode (MockEmbeddingModel).")
            config._embedding_model = MockEmbeddingModel()
            return config._embedding_model
        try:
            logger.info("Attempting to load local embedding model: all-MiniLM-L6-v2...")
            from sentence_transformers import SentenceTransformer
            config._embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
            logger.info("Local SentenceTransformer embedding model loaded successfully.")
        except Exception as e:
            if os.getenv("MEMORYOS_ALLOW_MOCK_MODELS", "false").lower() == "true":
                logger.warning(
                    "Embedding model failed to load; using the explicitly enabled non-semantic mock model: %s",
                    e,
                )
                config._embedding_model = MockEmbeddingModel()
            else:
                raise RuntimeError(
                    "Unable to load the semantic embedding model. Install the full model image or set "
                    "OFFLINE_MODE=true only for deterministic tests. To explicitly allow non-semantic "
                    "development mocks, set MEMORYOS_ALLOW_MOCK_MODELS=true."
                ) from e
    return config._embedding_model
