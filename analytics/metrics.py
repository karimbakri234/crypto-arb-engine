"""Per-stage latency histograms, hit rates, and PnL attribution.

Every stage of the pipeline (feed->state, state->detect, detect->decision,
decision->order-ack) is timed with `time.perf_counter_ns()` and recorded
here. A stage that exceeds its configured budget (`config.settings.
LATENCY_BUDGETS`) logs a warning immediately rather than silently
degrading. Metrics are exposed two ways: a periodic log dump
(`periodic_dump`) and a minimal `/metrics` HTTP endpoint
(`start_http_server`) serving a JSON snapshot -- deliberately built on
the standard library only (`http.server`) so metrics don't pull in a
heavier dependency than the rest of this engine needs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from config.settings import LATENCY_BUDGETS

logger = logging.getLogger(__name__)

_HISTOGRAM_MAXLEN = 20_000


@dataclass(slots=True)
class LatencyHistogram:
    """A bounded rolling window of latency samples (nanoseconds) for one stage."""

    samples: deque[int] = field(default_factory=lambda: deque(maxlen=_HISTOGRAM_MAXLEN))

    def record(self, latency_ns: int) -> None:
        self.samples.append(latency_ns)

    def percentile(self, p: float) -> float:
        if not self.samples:
            return 0.0
        ordered = sorted(self.samples)
        idx = min(int(len(ordered) * p), len(ordered) - 1)
        return ordered[idx] / 1e6  # -> milliseconds

    def summary(self) -> dict:
        if not self.samples:
            return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "max_ms": 0.0}
        return {
            "count": len(self.samples),
            "p50_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "p99_ms": self.percentile(0.99),
            "max_ms": max(self.samples) / 1e6,
        }


class MetricsRegistry:
    """Central collector for latency, hit-rate, and PnL-attribution metrics."""

    def __init__(self) -> None:
        self._latency: dict[str, LatencyHistogram] = {}
        self._hits: dict[str, int] = {}
        self._misses: dict[str, int] = {}
        self._pnl_by_strategy: dict[str, float] = {}
        self._http_server: ThreadingHTTPServer | None = None
        self._http_thread: threading.Thread | None = None

    # -- Latency ----------------------------------------------------------

    def record_latency(self, stage: str, latency_ns: int) -> None:
        histogram = self._latency.setdefault(stage, LatencyHistogram())
        histogram.record(latency_ns)

        budget_ns = getattr(LATENCY_BUDGETS, f"{stage}_ns", None)
        if budget_ns is not None and latency_ns > budget_ns:
            logger.warning(
                "Latency budget exceeded for stage=%s: %.3fms > budget %.3fms",
                stage, latency_ns / 1e6, budget_ns / 1e6,
            )

    def timed_stage(self, stage: str):
        """Context manager: `with metrics.timed_stage("state_to_detect"): ...`"""
        return _TimedStage(self, stage)

    # -- Hit rate -----------------------------------------------------------

    def record_hit(self, strategy: str, hit: bool) -> None:
        bucket = self._hits if hit else self._misses
        bucket[strategy] = bucket.get(strategy, 0) + 1

    def hit_rate(self, strategy: str) -> float:
        hits = self._hits.get(strategy, 0)
        misses = self._misses.get(strategy, 0)
        total = hits + misses
        return (hits / total) if total else 0.0

    # -- PnL attribution ------------------------------------------------------

    def record_pnl(self, strategy: str, pnl_usd: float) -> None:
        self._pnl_by_strategy[strategy] = self._pnl_by_strategy.get(strategy, 0.0) + pnl_usd

    # -- Reporting ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            "latency": {stage: h.summary() for stage, h in self._latency.items()},
            "hit_rate": {s: self.hit_rate(s) for s in set(self._hits) | set(self._misses)},
            "pnl_by_strategy_usd": dict(self._pnl_by_strategy),
            "taken_at": time.time(),
        }

    async def periodic_dump(self, interval_sec: float, stop_event: asyncio.Event) -> None:
        """Log a metrics snapshot every `interval_sec` seconds until `stop_event` is set."""
        while not stop_event.is_set():
            logger.info("metrics snapshot: %s", self.snapshot())
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_sec)
            except asyncio.TimeoutError:
                continue

    def start_http_server(self, port: int) -> None:
        """Serve `GET /metrics` (JSON snapshot) on a background thread."""
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
                if self.path != "/metrics":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps(registry.snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args) -> None:
                pass  # silence default stderr access logging

        self._http_server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        logger.info("Metrics HTTP server listening on :%d/metrics", port)

    def stop_http_server(self) -> None:
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server = None


class _TimedStage:
    """Context manager that times a block and records it under `stage`."""

    __slots__ = ("registry", "stage", "_start_ns")

    def __init__(self, registry: MetricsRegistry, stage: str) -> None:
        self.registry = registry
        self.stage = stage
        self._start_ns = 0

    def __enter__(self) -> "_TimedStage":
        self._start_ns = time.perf_counter_ns()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ns = time.perf_counter_ns() - self._start_ns
        self.registry.record_latency(self.stage, elapsed_ns)
