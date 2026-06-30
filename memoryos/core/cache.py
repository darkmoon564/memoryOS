import time

class STMCache:
    """
    Short-Term Memory (STM) cache layer.
    A simple Python dict-based LRU cache keyed by (user_id, workspace_id).
    Stores the last N ingested memory IDs and their content per user.
    Entries auto-expire after TTL seconds.
    """
    def __init__(self, max_items_per_user: int = 20, ttl_seconds: int = 7200):
        self.max_items = max_items_per_user
        self.ttl = ttl_seconds
        self._store = {}  # key: (user_id, workspace_id) -> list of {memory_id, content, embedding, timestamp}
    
    def push(self, user_id: str, workspace_id: str, memory_id: str, content: str, embedding=None):
        """Push a new memory into the STM cache."""
        key = (user_id, workspace_id)
        if key not in self._store:
            self._store[key] = []
        
        self._store[key].append({
            "memory_id": memory_id,
            "content": content,
            "embedding": embedding,
            "timestamp": time.time()
        })
        
        # Evict oldest if over capacity
        if len(self._store[key]) > self.max_items:
            self._store[key] = self._store[key][-self.max_items:]
    
    def get(self, user_id: str, workspace_id: str) -> list:
        """Get all non-expired STM items for a user/workspace."""
        key = (user_id, workspace_id)
        if key not in self._store:
            return []
        
        now = time.time()
        self._store[key] = [
            item for item in self._store[key]
            if (now - item["timestamp"]) < self.ttl
        ]
        return self._store[key]
    
    def clear(self, user_id: str, workspace_id: str):
        """Clear STM cache for a specific user/workspace."""
        key = (user_id, workspace_id)
        if key in self._store:
            del self._store[key]
            
    def remove(self, user_id: str, workspace_id: str, memory_id: str = None, content_sub: str = None):
        """Remove a specific memory item or any items matching a substring from the STM cache."""
        key = (user_id, workspace_id)
        if key in self._store:
            if memory_id:
                self._store[key] = [item for item in self._store[key] if item["memory_id"] != memory_id]
            elif content_sub:
                sub = content_sub.lower()
                self._store[key] = [item for item in self._store[key] if sub not in item["content"].lower()]

stm_cache = STMCache()
