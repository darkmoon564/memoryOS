import os
import re
from memoryos.config import logger

class MockRerankerModel:
    """Deterministic token overlap-based reranking fallback."""
    def predict(self, pairs):
        scores = []
        for query, doc in pairs:
            query_words = set(re.findall(r"\w+", query.lower()))
            doc_words = set(re.findall(r"\w+", doc.lower()))
            if not query_words:
                scores.append(0.0)
                continue
            overlap = query_words.intersection(doc_words)
            scores.append(float(len(overlap)) / len(query_words))
        return scores

def get_reranker_model():
    import memoryos.config as config
    if config._reranker_model is None:
        if os.getenv("OFFLINE_MODE", "false").lower() == "true":
            logger.info("OFFLINE_MODE is enabled. Operating in Zero-Dependency Mock ML mode (MockRerankerModel).")
            config._reranker_model = MockRerankerModel()
            return config._reranker_model
        try:
            logger.info("Attempting to load local reranker model: cross-encoder/ms-marco-MiniLM-L-6-v2...")
            from sentence_transformers import CrossEncoder
            config._reranker_model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
            logger.info("Local CrossEncoder reranker model loaded successfully.")
        except Exception as e:
            if os.getenv("MEMORYOS_ALLOW_MOCK_MODELS", "false").lower() == "true":
                logger.warning(
                    "Reranker model failed to load; using the explicitly enabled token-overlap mock model: %s",
                    e,
                )
                config._reranker_model = MockRerankerModel()
            else:
                raise RuntimeError(
                    "Unable to load the semantic reranker model. Install the full model image or set "
                    "OFFLINE_MODE=true only for deterministic tests. To explicitly allow non-semantic "
                    "development mocks, set MEMORYOS_ALLOW_MOCK_MODELS=true."
                ) from e
    return config._reranker_model
