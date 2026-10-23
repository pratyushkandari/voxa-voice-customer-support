import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from app.rag_engine import RAGEngine


def _async_iter(items):
    async def gen(*args, **kwargs):
        for item in items:
            yield item
    return gen


def _engine_with_cache(query_vec, cached_items):
    engine = RAGEngine(redis_url="redis://localhost:6379/0")
    engine.redis = AsyncMock()
    engine.generate_embedding = MagicMock(return_value=query_vec)
    keys = [f"semcache:{i}" for i in range(len(cached_items))]
    engine.redis.scan_iter = MagicMock(side_effect=_async_iter(keys))
    engine.redis.mget = AsyncMock(return_value=[json.dumps(item) for item in cached_items])
    engine.redis.incr = AsyncMock()
    return engine


def test_semantic_cache_hit_above_threshold():
    async def _test():
        # Mock query vector: unit vector along dimension 0
        mock_vec = [1.0] + [0.0] * 383
        engine = _engine_with_cache(mock_vec, [{
            "query": "What is your refund policy?",
            "vector": mock_vec,  # identical vector -> cosine sim = 1.0
            "response": "Refunds are processed within 30 days."
        }])

        cached_response = await engine.check_semantic_cache("What is your refund policy?", threshold=0.92)

        assert cached_response == "Refunds are processed within 30 days."
        engine.redis.incr.assert_called_with("metric:semantic_cache_hit_total")
        # Bounded SCAN must be used instead of the blocking KEYS command
        engine.redis.scan_iter.assert_called_once()
        engine.redis.keys.assert_not_called()

    asyncio.run(_test())


def test_semantic_cache_miss_below_threshold():
    async def _test():
        # Orthogonal vectors: cosine similarity = 0.0 < 0.92
        query_vec = [1.0] + [0.0] * 383
        cached_vec = [0.0, 1.0] + [0.0] * 382
        engine = _engine_with_cache(query_vec, [{
            "query": "How do I pair bluetooth headphones?",
            "vector": cached_vec,
            "response": "Hold the case button for 3 seconds."
        }])

        cached_response = await engine.check_semantic_cache("What is your refund policy?", threshold=0.92)

        assert cached_response is None
        engine.redis.incr.assert_called_with("metric:semantic_cache_miss_total")

    asyncio.run(_test())


def test_store_semantic_cache_persists_embedding_and_response():
    """Regression: store_semantic_cache referenced an undefined variable, so nothing was ever cached."""
    async def _test():
        engine = RAGEngine(redis_url="redis://localhost:6379/0")
        engine.redis = AsyncMock()
        vec = [0.6, 0.8] + [0.0] * 382
        engine.generate_embedding = MagicMock(return_value=vec)

        await engine.store_semantic_cache("What is your refund policy?", "30 days.")

        engine.redis.set.assert_awaited_once()
        key, raw = engine.redis.set.await_args.args
        assert key.startswith("semcache:")
        stored = json.loads(raw)
        assert stored == {"query": "What is your refund policy?", "vector": vec, "response": "30 days."}
        assert engine.redis.set.await_args.kwargs["ex"] == engine.cache_ttl

    asyncio.run(_test())


def test_store_then_lookup_round_trip_hits_cache():
    """A stored answer must be retrievable by the same question (end-to-end through the encoder)."""
    async def _test():
        engine = RAGEngine(redis_url="redis://localhost:6379/0")
        store = {}

        async def fake_set(key, value, ex=None):
            store[key] = value

        async def fake_mget(keys):
            return [store.get(k) for k in keys]

        engine.redis = AsyncMock()
        engine.redis.set = AsyncMock(side_effect=fake_set)
        engine.redis.mget = AsyncMock(side_effect=fake_mget)
        engine.redis.scan_iter = MagicMock(side_effect=lambda *a, **k: _async_iter(list(store))())

        await engine.store_semantic_cache("How long does shipping take?", "3 to 5 business days.")
        assert await engine.check_semantic_cache("How long does shipping take?") == "3 to 5 business days."
        assert await engine.check_semantic_cache("Can I pay with bitcoin?") is None

    asyncio.run(_test())
