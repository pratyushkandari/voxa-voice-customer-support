"""
Prometheus metrics, the correlation-ID / latency middleware and access-log redaction.

Metrics live in the process-local default registry, which is why the gateway runs a
single uvicorn worker per container and scales by adding replicas (each scraped
separately) instead of ``--workers N``.
"""
import logging
import re
import time
import uuid

from fastapi import Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

# ================= Prometheus Metrics Definition =================

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "endpoint", "status_code"],
    buckets=[0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
)

# Exposed as gemini_requests_total{outcome="ok"|"fallback"}
GEMINI_REQUESTS = Counter(
    "gemini_requests",
    "Replies Gemini was asked to write, by outcome (fallback = the plain reply was sent instead)",
    ["outcome"]
)

GEMINI_REQUEST_DURATION = Histogram(
    "gemini_request_duration_seconds",
    "Time from sending a request to Gemini until its reply finished streaming or failed",
    buckets=[0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 10.0]
)

# Exposed as semantic_cache_hit_total / semantic_cache_miss_total
SEMANTIC_CACHE_HITS = Counter(
    "semantic_cache_hit_total",
    "Total number of successful semantic cache hits"
)

SEMANTIC_CACHE_MISSES = Counter(
    "semantic_cache_miss_total",
    "Total number of semantic cache misses requiring retrieval or inference"
)

WEBSOCKET_ACTIVE_CONNECTIONS = Gauge(
    "websocket_active_connections",
    "Number of currently open WebSocket connections"
)


def gauge_value(gauge: Gauge) -> float:
    """Reads an unlabelled gauge through the public collect() API."""
    for metric in gauge.collect():
        for sample in metric.samples:
            return sample.value
    return 0.0

# ================= Correlation ID & Latency Middleware =================


class TelemetryMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Extract or generate X-Correlation-ID / trace_id
        trace_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request.state.trace_id = trace_id

        start_time = time.time()

        # 2. Process the request
        response: Response = await call_next(request)

        # 3. Calculate latency and record Prometheus metrics
        duration = time.time() - start_time

        # Label by the route *template* (e.g. /v1/tools/order/{order_id}), never the raw
        # path: raw paths put user-controlled IDs into label values and every distinct
        # value creates a new time series (unbounded memory / cardinality explosion).
        route = request.scope.get("route")
        endpoint = getattr(route, "path", None) or "unmatched"

        # Don't record metrics for the prometheus scraper itself
        if endpoint != "/metrics":
            HTTP_REQUEST_DURATION.labels(
                method=request.method,
                endpoint=endpoint,
                status_code=response.status_code
            ).observe(duration)

        # 4. Inject trace header into response
        response.headers["X-Correlation-ID"] = trace_id
        return response


def get_prometheus_metrics():
    """Returns Prometheus exposition format metrics."""
    return generate_latest(), CONTENT_TYPE_LATEST

# ================= Log Redaction =================

_TOKEN_QUERY_PARAM = re.compile(r"([?&]token=)[^&\s\"]+")


class RedactTokenFilter(logging.Filter):
    """
    Browsers cannot set headers on a WebSocket handshake, so the console sends its JWT as
    ``/v1/ws/stream?token=...`` and uvicorn logs that full path. This filter masks the
    token value so bearer credentials never reach log files or log shippers.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_QUERY_PARAM.sub(r"\1[REDACTED]", message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


def install_log_redaction() -> None:
    """Attaches ``RedactTokenFilter`` to uvicorn's loggers (idempotent)."""
    for name in ("uvicorn.error", "uvicorn.access"):
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactTokenFilter) for f in target.filters):
            target.addFilter(RedactTokenFilter())
