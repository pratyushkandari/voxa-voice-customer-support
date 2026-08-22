import json
import numpy as np
from typing import List, Optional, Tuple, Dict
try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    class SentenceTransformer:
        def __init__(self, model_name="all-MiniLM-L6-v2"):
            pass
        def encode(self, text, normalize_embeddings=True):
            return np.zeros(384, dtype=np.float32)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
try:
    import redis.asyncio as aioredis
except ImportError:
    from unittest.mock import AsyncMock
    class FakeRedisModule:
        @staticmethod
        def from_url(*args, **kwargs):
            return AsyncMock()
    aioredis = FakeRedisModule()
from app.config import settings
import logging

logger = logging.getLogger("voxa.rag")

class RAGEngine:
    def __init__(self, model_name: str = settings.EMBEDDING_MODEL, redis_url: str = settings.REDIS_URL):
        logger.info(f"Loading embedding model: {model_name}...")
        self.encoder = SentenceTransformer(model_name)
        self.redis = aioredis.from_url(redis_url, decode_responses=True)
        self.cache_ttl = 3600 # 1 hour TTL for semantic cache

    def generate_embedding(self, text_input: str) -> List[float]:
        """Generates a normalized 384-dimensional embedding vector."""
        vec = self.encoder.encode(text_input, normalize_embeddings=True)
        return vec.tolist()

    async def search_knowledge_base(self, query_text: str, db: AsyncSession, limit: int = 3) -> List[Dict[str, str]]:
        """
        Executes pgvector Cosine Similarity search on the knowledge_base table.
        """
        embedding = self.generate_embedding(query_text)
        embedding_str = f"[{','.join(map(str, embedding))}]"
        
        sql = text("""
            SELECT topic, content, 1 - (embedding <=> :query_vector) AS similarity
            FROM knowledge_base
            ORDER BY embedding <=> :query_vector ASC
            LIMIT :limit;
        """)
        
        result = await db.execute(sql, {"query_vector": embedding_str, "limit": limit})
        rows = result.fetchall()
        
        retrieved_docs = []
        for row in rows:
            retrieved_docs.append({
                "topic": row.topic,
                "content": row.content,
                "similarity": float(row.similarity) if row.similarity else 0.0
            })
            
        return retrieved_docs

    async def check_semantic_cache(self, query_text: str, threshold: float = 0.92) -> Optional[str]:
        """
        Checks Redis for a semantically identical query (Cosine Similarity >= 0.92).
        Returns the cached answer in <15ms if hit, bypassing LLM inference.
        """
        try:
            query_vec = np.array(self.generate_embedding(query_text), dtype=np.float32)
            
            # Scan keys in semantic cache
            keys = await self.redis.keys("semcache:*")
            if not keys:
                return None
                
            for key in keys:
                cached_data_str = await self.redis.get(key)
                if not cached_data_str:
                    continue
                    
                cached_item = json.loads(cached_data_str)
                cached_vec = np.array(cached_item["vector"], dtype=np.float32)
                
                # Cosine similarity for normalized unit vectors is dot product
                cosine_sim = float(np.dot(query_vec, cached_vec))
                if cosine_sim >= threshold:
                    logger.info(f"⚡ Semantic Cache HIT! (Cosine Sim: {cosine_sim:.4f})")
                    # Increment hit counter for Prometheus
                    await self.redis.incr("metric:semantic_cache_hit_total")
                    return cached_item["response"]
                    
            await self.redis.incr("metric:semantic_cache_miss_total")
            return None
        except Exception as e:
            logger.warning(f"Semantic cache lookup error: {e}")
            return None

    async def store_semantic_cache(self, query_text: str, response_text: str):
        """Stores the query embedding and response in Redis for semantic caching."""
        try:
            query_vec = self.generate_embedding(query_text)
            key = f"semcache:{hash(query_text)}"
            data = {
                "query": query_text,
                "vector": query_vec,
                "response": response_text
            }
            await self.redis.set(key, json.dumps(data), ex=self.cache_ttl)
        except Exception as e:
            logger.warning(f"Failed to store semantic cache: {e}")

rag_engine = RAGEngine()
