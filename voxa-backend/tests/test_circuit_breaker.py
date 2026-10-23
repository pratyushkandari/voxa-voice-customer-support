import asyncio
from unittest.mock import AsyncMock
from app.circuit_breaker import DistributedCircuitBreaker

def test_circuit_breaker_initial_closed_state():
    async def _test():
        cb = DistributedCircuitBreaker(redis_url="redis://localhost:6379/0", failure_threshold=0.5, recovery_timeout=15)
        cb.redis = AsyncMock()
        cb.redis.get = AsyncMock(return_value=None)
        state = await cb.get_state("test_service")
        assert state == "CLOSED"

    asyncio.run(_test())

def test_circuit_breaker_record_failure_trip():
    async def _test():
        cb = DistributedCircuitBreaker(redis_url="redis://localhost:6379/0", failure_threshold=0.5, recovery_timeout=15)
        cb.redis = AsyncMock()
        cb.redis.incr = AsyncMock(side_effect=[3, 4]) # fails=3, total=4 -> 75% failure rate
        cb.redis.expire = AsyncMock(return_value=True)
        cb.redis.set = AsyncMock(return_value=True)

        await cb.record_failure("test_service")

        cb.redis.set.assert_any_call("circuit:test_service:state", "OPEN")

    asyncio.run(_test())

def test_circuit_breaker_recovery_and_success():
    async def _test():
        cb = DistributedCircuitBreaker(redis_url="redis://localhost:6379/0", failure_threshold=0.5, recovery_timeout=15)
        cb.redis = AsyncMock()
        cb.redis.get = AsyncMock(return_value="HALF_OPEN")
        cb.redis.set = AsyncMock(return_value=True)
        cb.redis.delete = AsyncMock(return_value=True)

        await cb.record_success("test_service")

        cb.redis.set.assert_called_with("circuit:test_service:state", "CLOSED")
        cb.redis.delete.assert_any_call("circuit:test_service:failures")
        cb.redis.delete.assert_any_call("circuit:test_service:total")

    asyncio.run(_test())

def test_circuit_breaker_fallback_message():
    cb = DistributedCircuitBreaker(redis_url="redis://localhost:6379/0")
    msg = cb.get_fallback_response()
    assert "Voxa is currently experiencing unusually high demand" in msg
