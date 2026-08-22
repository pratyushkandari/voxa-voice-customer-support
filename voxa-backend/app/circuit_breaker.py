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
    def __init__(self, redis_url: str = settings.REDIS_URL, failure_threshold: float = 0.5, recovery_timeout: int = 15):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.failure_threshold = failure_threshold # 50% failure rate
        self.recovery_timeout = recovery_timeout   # 15 seconds cooldown before half-open

    async def get_state(self, service_name: str = "ai_worker") -> str:
        """Returns 'CLOSED', 'OPEN', or 'HALF_OPEN'."""
        state_key = f"circuit:{service_name}:state"
        last_trip_key = f"circuit:{service_name}:last_trip"
        
        state = await self.redis.get(state_key)
        if not state or state == "CLOSED":
            return "CLOSED"
            
        if state == "OPEN":
            last_trip = float(await self.redis.get(last_trip_key) or 0)
            if time.time() - last_trip > self.recovery_timeout:
                # Transition to HALF_OPEN to test worker health
                await self.redis.set(state_key, "HALF_OPEN")
                return "HALF_OPEN"
            return "OPEN"
            
        return state

    async def record_success(self, service_name: str = "ai_worker"):
        state = await self.get_state(service_name)
        if state == "HALF_OPEN":
            # Successfully recovered! Close the circuit
            await self.redis.set(f"circuit:{service_name}:state", "CLOSED")
            await self.redis.delete(f"circuit:{service_name}:failures")
            await self.redis.delete(f"circuit:{service_name}:total")

    async def record_failure(self, service_name: str = "ai_worker"):
        state_key = f"circuit:{service_name}:state"
        fail_key = f"circuit:{service_name}:failures"
        total_key = f"circuit:{service_name}:total"
        
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
