import time
import redis.asyncio as aioredis
from fastapi import Request, HTTPException, status
from app.config import settings

# Redis Lua Script for Atomic Sliding-Window Log Rate Limiting
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- Remove timestamps older than the current sliding window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- Count current requests in this active window
local current_requests = redis.call('ZCARD', key)

if current_requests < limit then
    -- Add the current timestamp with a unique member
    redis.call('ZADD', key, now, now .. '-' .. redis.call('INCR', key .. ':counter'))
    -- Set TTL to ensure memory cleanup
    redis.call('EXPIRE', key, window + 1)
    return 1
else
    return 0
end
"""

class RateLimiter:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.lua_script = None

    async def init_script(self):
        if not self.lua_script:
            self.lua_script = self.redis.register_script(SLIDING_WINDOW_LUA)

    async def check_rate_limit(self, identifier: str, limit: int = settings.RATE_LIMIT_REQUESTS_PER_MINUTE, window: int = 60) -> bool:
        await self.init_script()
        now = int(time.time())
        key = f"ratelimit:{identifier}"
        allowed = await self.lua_script(keys=[key], args=[now, window, limit])
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

async def rate_limit_dependency(request: Request):
    """FastAPI dependency for sliding-window rate limiting."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    user_id = request.headers.get("X-User-ID", client_ip)
    
    # 1. Idempotency Check if header present
    idempotency_key = request.headers.get("X-Idempotency-Key")
    if idempotency_key:
        is_first_attempt = await rate_limiter.check_idempotency(idempotency_key, user_id)
        if not is_first_attempt:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Duplicate request detected. Request is already processing or completed."
            )
            
    # 2. Rate Limit Check
    allowed = await rate_limiter.check_rate_limit(user_id)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down your requests.",
            headers={"Retry-After": "60"}
        )
