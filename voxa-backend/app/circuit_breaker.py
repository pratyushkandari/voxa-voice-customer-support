import logging
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger("voxa.circuit_breaker")


class DistributedCircuitBreaker:
    """
    Distributed 3-state Circuit Breaker (Martin Fowler pattern) backed by Redis, so every
    gateway replica observes the same state.

    States (stored at ``circuit:{service}:state``):
      - CLOSED: Requests execute normally. Outcomes are counted in a short window
        (``window_sec``, refreshed on every recorded outcome). Once at least
        ``min_calls`` outcomes are in the window and the failure ratio reaches
        ``failure_threshold``, the breaker trips to OPEN.
      - OPEN: Downstream is failing. All calls get an immediate fallback until
        ``recovery_timeout`` seconds have passed since the trip.
      - HALF_OPEN: Recovery probation. Exactly one canary request is admitted via an
        atomic Redis lease (SET NX EX); success closes the breaker, failure re-opens it.

    If Redis itself is unreachable the breaker fails open: ``can_execute`` admits the
    request with state ``UNKNOWN`` and outcome bookkeeping is skipped (logged), so a Redis
    outage never takes down answers that do not need Redis.
    """
    def __init__(
        self,
        redis_url: str = settings.REDIS_URL,
        failure_threshold: float = 0.5,
        recovery_timeout: int = 15,
        window_sec: int = 10,
        min_calls: int = 4,
    ):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.failure_threshold = failure_threshold  # 50% failure ratio trips breaker
        self.recovery_timeout = recovery_timeout    # seconds in OPEN before a canary probe
        self.window_sec = window_sec                # lifetime of the failure/total counters
        self.min_calls = min_calls                  # avoid tripping on 1 failure out of 1 call

    async def get_state(self, service_name: str = "ai_worker") -> str:
        """
        Retrieves the active breaker state, triggering auto-transition from OPEN to HALF_OPEN
        once the recovery cooldown expires.
        """
        state_key = f"circuit:{service_name}:state"
        last_trip_key = f"circuit:{service_name}:last_trip"

        state = await self.redis.get(state_key)
        if not state or state == "CLOSED":
            return "CLOSED"

        if state == "OPEN":
            last_trip = float(await self.redis.get(last_trip_key) or 0)
            if time.time() - last_trip > self.recovery_timeout:
                # Transition to HALF_OPEN to allow a controlled canary probe
                await self.redis.set(state_key, "HALF_OPEN")
                return "HALF_OPEN"
            return "OPEN"

        return state

    async def can_execute(self, service_name: str = "ai_worker") -> tuple[bool, str]:
        """
        Gating check for ingress requests.
        Returns:
            (allowed: bool, state: str)
        In HALF_OPEN state, uses an atomic Redis mutex ('probe_lock') with a 10s TTL
        so that exactly one request is dispatched to probe downstream health.
        """
        try:
            state = await self.get_state(service_name)
        except RedisError as e:
            logger.warning(f"Circuit breaker state unavailable, admitting request: {e}")
            return True, "UNKNOWN"
        if state == "CLOSED":
            return True, "CLOSED"

        if state == "OPEN":
            return False, "OPEN"

        if state == "HALF_OPEN":
            # Concurrency gate: allow only ONE probe request to test downstream recovery
            probe_key = f"circuit:{service_name}:probe_lock"
            try:
                acquired = await self.redis.set(probe_key, "1", nx=True, ex=10)
                return bool(acquired), "HALF_OPEN"
            except Exception:
                # Fail open: if Redis cannot arbitrate the probe, let the request through
                # rather than blocking all traffic indefinitely.
                return True, "HALF_OPEN"

        return True, state

    async def _count_outcome(self, service_name: str, failed: bool) -> tuple[int, int]:
        """Bumps the windowed counters and returns (failures, total) after the update."""
        fail_key = f"circuit:{service_name}:failures"
        total_key = f"circuit:{service_name}:total"
        if failed:
            fails = await self.redis.incr(fail_key)
            await self.redis.expire(fail_key, self.window_sec)
        else:
            fails = await self.redis.get(fail_key)
        total = await self.redis.incr(total_key)
        await self.redis.expire(total_key, self.window_sec)
        return int(fails or 0), int(total or 0)

    async def record_success(self, service_name: str = "ai_worker"):
        try:
            await self._record_success(service_name)
        except RedisError as e:
            logger.warning(f"Could not record circuit breaker success: {e}")

    async def record_failure(self, service_name: str = "ai_worker"):
        try:
            await self._record_failure(service_name)
        except RedisError as e:
            logger.warning(f"Could not record circuit breaker failure: {e}")

    async def _record_success(self, service_name: str):
        state_key = f"circuit:{service_name}:state"
        state = await self.get_state(service_name)
        if state == "HALF_OPEN":
            # Successfully recovered! Close the circuit and reset metrics
            await self.redis.set(state_key, "CLOSED")
            await self.redis.delete(f"circuit:{service_name}:failures")
            await self.redis.delete(f"circuit:{service_name}:total")
            try:
                await self.redis.delete(f"circuit:{service_name}:probe_lock")
            except Exception:
                pass
            return

        if state == "CLOSED":
            # Successes must count towards the denominator, otherwise the "failure ratio"
            # would be failures/failures (=100%) and any 4 errors would trip the breaker.
            await self._count_outcome(service_name, failed=False)

    async def _record_failure(self, service_name: str):
        state_key = f"circuit:{service_name}:state"

        current_state = await self.redis.get(state_key)
        if current_state == "HALF_OPEN":
            # Probe failed; immediately trip back to OPEN with fresh timeout
            await self.redis.set(state_key, "OPEN")
            await self.redis.set(f"circuit:{service_name}:last_trip", str(time.time()))
            try:
                await self.redis.delete(f"circuit:{service_name}:probe_lock")
            except Exception:
                pass
            return

        fails, total = await self._count_outcome(service_name, failed=True)

        if total >= self.min_calls and (fails / total) >= self.failure_threshold:
            # Trip the circuit to OPEN
            await self.redis.set(state_key, "OPEN")
            await self.redis.set(f"circuit:{service_name}:last_trip", str(time.time()))

    def get_fallback_response(self) -> str:
        return (
            "⚠️ Voxa is currently experiencing unusually high demand. "
            "Our automated AI inference cluster is self-protecting. "
            "Please check our standard FAQs at https://support.voxa.ai/faq "
            "or an agent will review your inquiry shortly."
        )


circuit_breaker = DistributedCircuitBreaker()
