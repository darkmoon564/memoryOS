"""Small dependency-free observability primitives for the service."""

import threading
from collections import defaultdict
from contextvars import ContextVar


request_id: ContextVar[str] = ContextVar("request_id", default="-")


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(lambda: [0.0, 0.0])

    @staticmethod
    def _key(name: str, labels: dict[str, object] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))

    def increment(self, name: str, labels: dict[str, object] | None = None, amount: float = 1.0) -> None:
        with self._lock:
            self._counters[self._key(name, labels)] += amount

    def observe(self, name: str, value: float, labels: dict[str, object] | None = None) -> None:
        with self._lock:
            bucket = self._histograms[self._key(name, labels)]
            bucket[0] += value
            bucket[1] += 1

    def render_prometheus(self) -> str:
        def labels_text(labels: tuple[tuple[str, str], ...]) -> str:
            if not labels:
                return ""
            escaped = (f'{key}="{value.replace(chr(34), chr(92) + chr(34))}"' for key, value in labels)
            return "{" + ",".join(escaped) + "}"

        lines: list[str] = []
        with self._lock:
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"{name}{labels_text(labels)} {value}")
            for (name, labels), (total, count) in sorted(self._histograms.items()):
                lines.append(f"{name}_sum{labels_text(labels)} {total}")
                lines.append(f"{name}_count{labels_text(labels)} {count}")
        return "\n".join(lines) + "\n"


metrics = MetricsRegistry()
