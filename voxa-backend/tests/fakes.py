"""Small stand-ins for Redis and the database session, shared by the unit tests."""
from redis.exceptions import ConnectionError as RedisConnectionError


class FakeRedis:
    """Tiny in-memory stand-in for the string commands the circuit breaker uses."""
    def __init__(self):
        self.data = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key, 0)) + 1)
        return int(self.data[key])

    async def expire(self, key, seconds):
        return True

    async def delete(self, key):
        self.data.pop(key, None)


class UnreachableRedis:
    """Every command fails the way redis-py does when the server is down."""
    def __getattr__(self, name):
        async def _fail(*args, **kwargs):
            raise RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
        return _fail


class DeadPubSub:
    """A Pub/Sub connection whose subscribe fails, as when Redis is down."""
    async def subscribe(self, *args):
        raise RedisConnectionError("Connection refused")

    async def unsubscribe(self, *args):
        pass

    async def aclose(self):
        pass


class DummySession:
    """Stands in for AsyncSessionLocal() when the tools it is passed to are mocked."""
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False
