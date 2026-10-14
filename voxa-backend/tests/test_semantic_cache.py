import pytest
import asyncio
import json
import numpy as np
from unittest.mock import AsyncMock, patch, MagicMock
from app.rag_engine import RAGEngine

def test_semantic_cache_hit_above_threshold():
    async def _test():
        engine = RAGEngine(redis_url="redis://localhost:6379/0")
        engine.redis = AsyncMock()

        # Mock query vector: unit vector along dimension 0
        mock_vec = [1.0] + [0.0] * 383
        engine.generate_embedding = MagicMock(return_value=mock_vec)

        # Mock cached entry with identical vector (cosine sim = 1.0)
        cached_item = {
            "query": "What is your refund policy?",
            "vector": mock_vec,
            "response": "Refunds are processed within 30 days."
        }
        engine.redis.keys = AsyncMock(return_value=["semcache:12345"])
        engine.redis.get = AsyncMock(return_value=json.dumps(cached_item))
        engine.redis.incr = AsyncMock()

        cached_response = await engine.check_semantic_cache("What is your refund policy?", threshold=0.92)

        assert cached_response == "Refunds are processed within 30 days."
        engine.redis.incr.assert_called_with("metric:semantic_cache_hit_total")

    asyncio.run(_test())

def test_semantic_cache_miss_below_threshold():
    async def _test():
        engine = RAGEngine(redis_url="redis://localhost:6379/0")
        engine.redis = AsyncMock()

        # Orthogonal vectors: cosine similarity = 0.0 < 0.92
        query_vec = [1.0] + [0.0] * 383
        cached_vec = [0.0, 1.0] + [0.0] * 382

        engine.generate_embedding = MagicMock(return_value=query_vec)

        cached_item = {
            "query": "How do I pair bluetooth headphones?",
            "vector": cached_vec,
            "response": "Hold the case button for 3 seconds."
        }
        engine.redis.keys = AsyncMock(return_value=["semcache:99999"])
        engine.redis.get = AsyncMock(return_value=json.dumps(cached_item))
        engine.redis.incr = AsyncMock()

        cached_response = await engine.check_semantic_cache("What is your refund policy?", threshold=0.92)

        assert cached_response is None
        engine.redis.incr.assert_called_with("metric:semantic_cache_miss_total")

    asyncio.run(_test())
