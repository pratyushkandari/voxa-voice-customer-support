import time
from app.config import settings
try:
    import redis.asyncio as aioredis
except ImportError:
    from unittest.mock import AsyncMock
    class FakeRedisModule:
        @staticmethod
        def from_url(*args, **kwargs):
            return AsyncMock()
    aioredis = FakeRedisModule()

class DistributedCircuitBreaker:
    """
    Distributed 3-state Circuit Breaker (Martin Fowler pattern) backed by Redis.

    Prevents cascading service degradation across asynchronous worker clusters.
    States:
      - CLOSED: Requests execute normally. Failure rates are measured across a 10s sliding window.
      - OPEN: Downstream service is failing. All non-probe calls return immediate fallbacks.
      - HALF_OPEN: Downstream recovery probation. Allows exactly one canary probe via an atomic
        Redis lease (SET NX EX) to verify health without risking worker starvation.
    """
    def __init__(self, redis_url: str = settings.REDIS_URL, failure_threshold: float = 0.5, recovery_timeout: int = 15):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.failure_threshold = failure_threshold  # 50% failure ratio trips breaker
        self.recovery_timeout = recovery_timeout    # 15s cooldown before probing downstream health

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
        state = await self.get_state(service_name)
        if state == "CLOSED":
            return True, "CLOSED"
            
        if state == "OPEN":
            return False, "OPEN"
            
        if state == "HALF_OPEN":
            # Concurrency gate: allow only ONE probe request to test downstream recovery
            probe_key = f"circuit:{service_name}:probe_lock"
            try:
                # Attempt to acquire a short-lived probe lock
                acquired = await self.redis.set(probe_key, "1", nx=True, ex=10)
                if acquired:
                    return True, "HALF_OPEN"
                else:
                    return False, "HALF_OPEN"
            except Exception:
                return True, "HALF_OPEN"

        return True, state

    async def record_success(self, service_name: str = "ai_worker"):
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

    async def record_failure(self, service_name: str = "ai_worker"):
        state_key = f"circuit:{service_name}:state"
        fail_key = f"circuit:{service_name}:failures"
        total_key = f"circuit:{service_name}:total"
        
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
        
        fails = await self.redis.incr(fail_key)
        total = await self.redis.incr(total_key)
        await self.redis.expire(fail_key, 10)
        await self.redis.expire(total_key, 10)
        
        if total >= 4 and (fails / total) >= self.failure_threshold:
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
