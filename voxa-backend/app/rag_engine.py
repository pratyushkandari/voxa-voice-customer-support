import asyncio
import hashlib
import json
import logging
from typing import Dict, List, Optional

import numpy as np
import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.telemetry import SEMANTIC_CACHE_HITS, SEMANTIC_CACHE_MISSES

logger = logging.getLogger("voxa.rag")

# Upper bound on cache entries compared per lookup. SCAN returns keys in hash-slot order,
# so this is an arbitrary (not most-recent) sample: it caps the O(N) comparison cost and
# Redis round-trips at the price of possibly missing a match in a very large cache.
SEMANTIC_CACHE_SCAN_LIMIT = 50


class RAGEngine:
    """
    Two-Tier Hybrid Retrieval Engine:
      1. Tier 1: Redis Semantic Vector Cache (normalized embeddings, cosine == dot product).
      2. Tier 2: PostgreSQL 16 + pgvector HNSW Approximate Nearest Neighbor (ANN) search.

    The sentence-transformer model is loaded lazily on first use and inference runs in a
    worker thread, so importing this module is cheap and embedding a query never blocks
    the asyncio event loop that serves every other WebSocket.
    """
    def __init__(self, model_name: str = settings.EMBEDDING_MODEL, redis_url: str = settings.REDIS_URL):
        self.model_name = model_name
        self._encoder = None
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.cache_ttl = 3600  # 1 hour TTL for public policy semantic cache

    @property
    def encoder(self):
        if self._encoder is None:
            from sentence_transformers import SentenceTransformer  # heavy import, deferred
            logger.info(f"Loading embedding model: {self.model_name}...")
            self._encoder = SentenceTransformer(self.model_name)
        return self._encoder

    def generate_embedding(self, text_input: str) -> List[float]:
        """
        Generates a normalized 384-dimensional dense embedding vector (CPU-bound; blocking).
        Normalization ensures cosine similarity reduces to a fast dot-product operation.
        """
        vec = self.encoder.encode(text_input, normalize_embeddings=True)
        return np.asarray(vec, dtype=np.float32).tolist()

    async def generate_embedding_async(self, text_input: str) -> List[float]:
        """Non-blocking wrapper around ``generate_embedding`` for use on the event loop."""
        return await asyncio.to_thread(self.generate_embedding, text_input)

    async def search_knowledge_base(self, query_text: str, db: AsyncSession, limit: int = 3) -> List[Dict[str, str]]:
        """
        Executes approximate nearest neighbor (ANN) vector search on PostgreSQL pgvector
        using the HNSW index with the cosine distance operator (<=>).
        Returns up to ``limit`` dicts of {topic, content, similarity} ordered best-first.
        """
        embedding = await self.generate_embedding_async(query_text)
        embedding_str = f"[{','.join(map(str, embedding))}]"

        sql = text("""
            SELECT topic, content, 1 - (embedding <=> CAST(:query_vector AS vector)) AS similarity
            FROM knowledge_base
            ORDER BY embedding <=> CAST(:query_vector AS vector) ASC
            LIMIT :limit;
        """)

        result = await db.execute(sql, {"query_vector": embedding_str, "limit": limit})
        rows = result.fetchall()

        return [
            {
                "topic": row.topic,
                "content": row.content,
                "similarity": float(row.similarity) if row.similarity is not None else 0.0
            }
            for row in rows
        ]

    @staticmethod
    def _cache_key(query_text: str) -> str:
        query_hash = hashlib.sha256(query_text.strip().lower().encode("utf-8")).hexdigest()[:16]
        return f"semcache:{query_hash}"

    async def check_semantic_cache(self, query_text: str, threshold: float = 0.92) -> Optional[str]:
        """
        Returns a cached response whose query embedding has cosine similarity >= threshold,
        or None. Uses incremental SCAN (never the blocking KEYS command) bounded to
        SEMANTIC_CACHE_SCAN_LIMIT keys, and a single MGET for the candidate payloads.
        Any Redis/model error degrades to a cache miss rather than failing the request.
        """
        try:
            query_vec = np.array(await self.generate_embedding_async(query_text), dtype=np.float32)

            keys = []
            async for key in self.redis.scan_iter(match="semcache:*", count=SEMANTIC_CACHE_SCAN_LIMIT):
                keys.append(key)
                if len(keys) >= SEMANTIC_CACHE_SCAN_LIMIT:
                    break

            cached_payloads = await self.redis.mget(keys) if keys else []
            for cached_data_str in cached_payloads:
                if not cached_data_str:
                    continue  # expired between SCAN and MGET

                cached_item = json.loads(cached_data_str)
                cached_vec = np.array(cached_item["vector"], dtype=np.float32)

                # Cosine similarity for normalized unit vectors is dot product
                cosine_sim = float(np.dot(query_vec, cached_vec))
                if cosine_sim >= threshold:
                    logger.info(f"⚡ Semantic Cache HIT! (Cosine Sim: {cosine_sim:.4f})")
                    SEMANTIC_CACHE_HITS.inc()
                    # Redis counters are shared by all replicas and feed /v1/admin/metrics
                    await self.redis.incr("metric:semantic_cache_hit_total")
                    return cached_item["response"]

            SEMANTIC_CACHE_MISSES.inc()
            await self.redis.incr("metric:semantic_cache_miss_total")
            return None
        except Exception as e:
            logger.warning(f"Semantic cache lookup error: {e}")
            return None

    async def store_semantic_cache(self, query_text: str, response_text: str):
        """
        Stores the query embedding and response in Redis for semantic caching.
        Only call this for generic, user-independent answers (never order or ticket data).
        """
        try:
            query_vec = await self.generate_embedding_async(query_text)
            data = {
                "query": query_text,
                "vector": query_vec,
                "response": response_text
            }
            await self.redis.set(self._cache_key(query_text), json.dumps(data), ex=self.cache_ttl)
        except Exception as e:
            logger.warning(f"Failed to store semantic cache: {e}")


rag_engine = RAGEngine()
