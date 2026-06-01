"""
Observability layer: structured logging, distributed tracing, metrics.

Design decisions (for interview discussion):
- OpenTelemetry-compatible trace context propagated through agent sessions
- Structured JSON logging for machine-readability (not printf-style)
- Latency histograms per pipeline stage for performance regression detection
- Trace IDs link webhook → review → PR comment end-to-end

Why not just `print()`?
"printf debugging doesn't scale to production. With structured logging
and trace IDs, I can answer 'how long does the verify stage take at P99?'
or 'what PR triggered this error?' from the logs without grep."
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

# ---- Structured logging -----------------------------------------------------


class JsonFormatter(logging.Formatter):
    """JSON log formatter for machine-parsable logs."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "trace_id"):
            entry["trace_id"] = record.trace_id
        if hasattr(record, "span_id"):
            entry["span_id"] = record.span_id
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json.dumps(entry, default=str)


# ---- Tracing -----------------------------------------------------------------


@dataclass
class Span:
    """A single operation span in a trace."""
    name: str
    trace_id: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: str | None = None
    started_at: float = field(default_factory=time.time)
    attributes: dict[str, Any] = field(default_factory=dict)
    _ended: bool = False

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def end(self) -> None:
        if not self._ended:
            duration = time.time() - self.started_at
            log.info(
                "span=%s trace=%s duration=%.3fs %s",
                self.name, self.trace_id, duration,
                " ".join(f"{k}={v}" for k, v in self.attributes.items()),
            )
            self._ended = True


class Tracer:
    """Minimal tracer that logs spans as structured JSON.

    In production, swap for OpenTelemetry SDK. The interface is compatible.
    """

    def __init__(self):
        self._current_trace_id: str | None = None

    @contextmanager
    def trace(self, trace_id: str | None = None):
        """Start a new trace. All spans within the context share the trace_id."""
        old = self._current_trace_id
        self._current_trace_id = trace_id or uuid.uuid4().hex[:16]
        try:
            yield self._current_trace_id
        finally:
            self._current_trace_id = old

    @contextmanager
    def span(self, name: str, **attrs):
        """Create a span within the current trace."""
        if self._current_trace_id is None:
            self._current_trace_id = uuid.uuid4().hex[:16]

        span = Span(
            name=name,
            trace_id=self._current_trace_id,
        )
        for k, v in attrs.items():
            span.set_attribute(k, v)
        try:
            yield span
        finally:
            span.end()


# Global tracer instance
tracer = Tracer()


# ---- Metrics -----------------------------------------------------------------


@dataclass
class PipelineMetrics:
    """Aggregated metrics for review pipeline performance."""
    total_reviews: int = 0
    total_errors: int = 0
    stage_latencies: dict[str, list[float]] = field(default_factory=dict)
    finding_counts: list[int] = field(default_factory=list)

    def record_review(
        self,
        duration_ms: float,
        stage_timings: dict[str, float],
        finding_count: int,
        success: bool,
    ) -> None:
        self.total_reviews += 1
        if not success:
            self.total_errors += 1
        self.finding_counts.append(finding_count)
        for stage, duration in stage_timings.items():
            if stage not in self.stage_latencies:
                self.stage_latencies[stage] = []
            self.stage_latencies[stage].append(duration)

    def p50(self, values: list[float]) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        mid = len(sorted_vals) // 2
        return sorted_vals[mid]

    def p99(self, values: list[float]) -> float:
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * 0.99)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

    def summary(self) -> dict[str, Any]:
        return {
            "total_reviews": self.total_reviews,
            "error_rate": (
                self.total_errors / self.total_reviews
                if self.total_reviews > 0
                else 0.0
            ),
            "avg_findings_per_review": (
                sum(self.finding_counts) / len(self.finding_counts)
                if self.finding_counts
                else 0.0
            ),
            "stage_latency_p50": {
                s: self.p50(v) for s, v in self.stage_latencies.items()
            },
            "stage_latency_p99": {
                s: self.p99(v) for s, v in self.stage_latencies.items()
            },
        }


# Global metrics instance
metrics = PipelineMetrics()


# ---- Setup -------------------------------------------------------------------


def setup_logging(level: int = logging.INFO) -> None:
    """Configure structured JSON logging."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


# Auto-configure if not already set up
if not logging.getLogger().handlers:
    setup_logging()

log = logging.getLogger("open-reviewer.observability")
