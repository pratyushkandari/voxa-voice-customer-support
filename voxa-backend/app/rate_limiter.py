"""
Redis-backed sliding-window-log rate limiter and idempotency-key guard.

Algorithm: each request is recorded as a member of a per-identity sorted set scored by
its timestamp. A Lua script atomically evicts entries older than the window, counts the
rest, and admits the request only if the count is below the limit, so concurrent gateway
replicas share one consistent budget without race conditions.
"""
import time
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, Request, status

from app.auth import decode_access_token
from app.config import settings

# KEYS[1] = sorted-set log, KEYS[2] = monotonic counter used to make members unique.
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local counter_key = KEYS[2]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- Remove timestamps older than the current sliding window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- Count current requests in this active window
local current_requests = redis.call('ZCARD', key)

if current_requests < limit then
    -- Add the current timestamp with a unique member
    redis.call('ZADD', key, now, now .. '-' .. redis.call('INCR', counter_key))
    -- Expire both keys so idle identities do not leak memory
    redis.call('EXPIRE', key, window + 1)
    redis.call('EXPIRE', counter_key, window + 1)
    return 1
else
    return 0
end
"""


class RateLimiter:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.lua_script = self.redis.register_script(SLIDING_WINDOW_LUA)

    async def check_rate_limit(
        self,
        identifier: str,
        limit: int = settings.RATE_LIMIT_REQUESTS_PER_MINUTE,
        window: int = 60,
    ) -> bool:
        """Returns True if the request is admitted, False if ``identifier`` is over budget."""
        now = int(time.time())
        key = f"ratelimit:{identifier}"
        allowed = await self.lua_script(keys=[key, f"{key}:counter"], args=[now, window, limit])
        return bool(allowed)

    async def check_idempotency(self, idempotency_key: str, user_id: str, ttl: int = 60) -> bool:
        """
        Checks if an X-Idempotency-Key has already been processed.
        Returns True if this is a NEW request, False if it is a duplicate retry.
        """
        key = f"idempotency:{user_id}:{idempotency_key}"
        is_new = await self.redis.set(key, "PROCESSING", nx=True, ex=ttl)
        return bool(is_new)


rate_limiter = RateLimiter()


def client_identity(client_host: Optional[str], headers, token: Optional[str]) -> str:
    """
    Resolves the rate-limit identity: the JWT subject when a valid token is supplied,
    otherwise the client IP (X-Forwarded-For is honoured only when TRUST_FORWARDED_FOR).
    """
    if token:
        payload = decode_access_token(token)
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"

    client_ip = client_host or "unknown"
    forwarded_for = headers.get("X-Forwarded-For") if headers is not None else None
    if settings.TRUST_FORWARDED_FOR and forwarded_for:
        client_ip = forwarded_for.split(",")[0].strip()
    return f"ip:{client_ip}"


async def rate_limit_dependency(request: Request):
    """FastAPI dependency for sliding-window rate limiting with verified identity."""
    token = None
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
    identity = client_identity(request.client.host if request.client else None, request.headers, token)

    # 1. Rate limit first, so a rejected (429) request does not burn its idempotency key
    #    and can be retried with the same key after Retry-After.
    allowed = await rate_limiter.check_rate_limit(identity)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down your requests.",
            headers={"Retry-After": "60"}
        )

    # 2. Idempotency check if header present
    idempotency_key = request.headers.get("X-Idempotency-Key")
    if idempotency_key:
        is_first_attempt = await rate_limiter.check_idempotency(idempotency_key, identity)
        if not is_first_attempt:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Duplicate request detected. Request is already processing or completed."
            )
