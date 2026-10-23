"""
Prometheus metrics and the correlation-ID / latency middleware.

Metrics live in the process-local default registry, which is why the gateway runs a
single uvicorn worker per container and scales by adding replicas (each scraped
separately) instead of ``--workers N``.
"""
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

LLM_TTFT_HISTOGRAM = Histogram(
    "llm_time_to_first_token_seconds",
    "Time to first token (TTFT) in seconds for LLM inference",
    buckets=[0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0]
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
