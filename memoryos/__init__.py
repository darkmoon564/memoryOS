"""MemoryOS package.

The ASGI application is exported lazily so operational commands such as
`python -m memoryos.migrations` do not import server routes as a side effect.
"""


def __getattr__(name):
    if name == "app":
        from memoryos.main import app
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
