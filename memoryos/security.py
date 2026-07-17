"""Security primitives used at the application boundary."""

import hashlib
import secrets


API_KEY_PREFIX = "memos_"


def hash_api_key(api_key: str) -> str:
    """Return the stable lookup digest for an API key without storing it."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    """Generate a high-entropy key. Display it once, then persist only its hash."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
