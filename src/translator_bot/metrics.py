"""In-process metrics: counters, latency summaries and provider usage. Never records message content."""

from __future__ import annotations

import time
from collections import defaultdict, deque


class Metrics:
    def __init__(self, sample_size: int = 512) -> None:
        self.started = time.time()
        self.counters: dict[str, int] = defaultdict(int)
        self._samples: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=sample_size))
        self.usage: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def inc(self, name: str, n: int = 1) -> None:
        self.counters[name] += n

    def observe(self, name: str, value_ms: float) -> None:
        self._samples[name].append(value_ms)

    def add_usage(self, provider: str, model: str, tin: int, tout: int, chars: int) -> None:
        u = self.usage[f"{provider}:{model}"]
        u["requests"] += 1
        u["input_tokens"] += tin
        u["output_tokens"] += tout
        u["billed_chars"] += chars

    def percentiles(self, name: str) -> dict[str, float]:
        s = sorted(self._samples.get(name, ()))
        if not s:
            return {}

        def pct(p: float) -> float:
            return round(s[min(len(s) - 1, int(p * (len(s) - 1)))], 1)

        return {"n": len(s), "p50": pct(0.5), "p95": pct(0.95), "max": round(s[-1], 1)}

    def snapshot(self) -> dict[str, object]:
        return {
            "uptime_s": int(time.time() - self.started),
            "counters": dict(sorted(self.counters.items())),
            "latency_ms": {k: self.percentiles(k) for k in sorted(self._samples)},
            "usage": {k: dict(v) for k, v in self.usage.items()},
        }
