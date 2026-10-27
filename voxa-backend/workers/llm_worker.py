"""
LLM ("reasoning") worker: consumes prompts from ``stream:llm:inference`` (group
``llm_group``), produces an answer and streams it word-by-word to the session's Pub/Sub
channel ``token:stream:{session_id}``. Voice-originated prompts are also forwarded to
the TTS stream.

Answer generation is deterministic: prompts go through ``answer_text_query``, the same
priority-ordered router the gateway uses for WebSocket text queries (guardrails, tools
against PostgreSQL, canned policies, semantic cache, pgvector knowledge base), so REST,
voice and console users get identical answers. No generative model is involved.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import List, Optional, Tuple

import redis.asyncio as aioredis

from app.agent_tools import answer_text_query
from app.config import settings
from app.queue_manager import keep_claimed, session_channel
from app.rag_engine import rag_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.llm_worker")

MAX_ATTEMPTS = 3


class LLMWorker:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"llm-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_LLM_INFERENCE
        self.group_name = "llm_group"
        self.batch_window_ms = 15  # XREADGROUP block time used to coalesce a micro-batch
        self.max_batch_size = 8
        logger.info(f"🧠 LLM worker {self.worker_id} ready (deterministic router + pgvector RAG).")

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:llm:{self.worker_id}", str(time.time()), ex=10)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
            await asyncio.sleep(5)

    async def generate_response(self, prompt: str, user_id: Optional[str]) -> Tuple[str, bool]:
        """
        Answers an already-sanitised prompt (the gateway and the STT worker sanitise at
        ingress) with the shared router; returns ``(text, cache_hit)``.
        """
        parsed_uid = None
        if user_id and user_id != "guest":
            try:
                parsed_uid = uuid.UUID(str(user_id))
            except (ValueError, TypeError):
                parsed_uid = None
        return await answer_text_query(prompt, prompt, parsed_uid)

    async def process_message(self, msg_id: str, payload: dict, redis: aioredis.Redis):
        """Handles one task end-to-end. Never raises: failures are retried, then DLQ'd."""
        session_id = payload.get("session_id", "unknown")
        trace_id = payload.get("trace_id", "unknown")
        retry_count = int(payload.get("retry_count", 0))
        channel = session_channel(session_id)
        start_time = time.time()

        try:
            response_text, cache_hit = await self.generate_response(payload.get("prompt", ""), payload.get("user_id"))
        except Exception as e:
            logger.error(f"[{trace_id}] Task {msg_id} failed (attempt {retry_count + 1}): {e}")
            if retry_count + 1 >= MAX_ATTEMPTS:
                await redis.xadd(settings.STREAM_DLQ, {
                    **payload, "origin_stream": self.stream_name, "error": str(e), "failed_worker": self.worker_id
                }, maxlen=settings.STREAM_MAXLEN, approximate=True)
                await redis.publish(channel, json.dumps({
                    "type": "error",
                    "message": "Sorry, something went wrong while answering. Please try again.",
                    "trace_id": trace_id,
                }))
            else:
                await redis.xadd(self.stream_name, {**payload, "retry_count": str(retry_count + 1)},
                                 maxlen=settings.STREAM_MAXLEN, approximate=True)
            await redis.xack(self.stream_name, self.group_name, msg_id)
            return

        # Voice pipeline: ask the TTS worker to synthesise the answer
        if payload.get("origin") == "audio" or payload.get("needs_audio") == "true":
            try:
                await redis.xadd(settings.STREAM_TTS_SYNTHESIS, {
                    "session_id": session_id,
                    "text": response_text,
                    "trace_id": trace_id
                }, maxlen=settings.STREAM_MAXLEN, approximate=True)
            except Exception as tts_err:
                logger.warning(f"Could not enqueue TTS synthesis task: {tts_err}")

        # Stream tokens to Redis Pub/Sub (the gateway relays them to the client)
        ttft_ms = int((time.time() - start_time) * 1000)
        for token in response_text.split(" "):
            await redis.publish(channel, json.dumps({
                "type": "token_chunk",
                "token": token + " ",
                "is_final": False,
                "ttft_ms": ttft_ms,
                "cache_hit": cache_hit,
                "trace_id": trace_id
            }))
            await asyncio.sleep(0.03)

        await redis.publish(channel, json.dumps({
            "type": "token_chunk",
            "token": "",
            "is_final": True,
            "ttft_ms": ttft_ms,
            "total_latency_ms": int((time.time() - start_time) * 1000),
            "cache_hit": cache_hit,
            "trace_id": trace_id
        }))

        await redis.xack(self.stream_name, self.group_name, msg_id)

    async def process_batch(self, batch: List[Tuple[str, dict]], redis: aioredis.Redis):
        """
        Processes a micro-batch (up to max_batch_size tasks) sequentially, in arrival order.
        Entries still waiting their turn are kept claimed so the supervisor does not mistake
        them for stranded work and enqueue duplicates.
        """
        logger.info(f"⚡ Processing micro-batch of {len(batch)} items...")
        claim_task = asyncio.create_task(
            keep_claimed(redis, self.stream_name, self.group_name, self.worker_id, [m for m, _ in batch])
        )
        try:
            for msg_id, payload in batch:
                await self.process_message(msg_id, payload, redis)
        finally:
            claim_task.cancel()

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass  # BUSYGROUP: group already exists

        # Load the embedding model before taking work, as the gateway does at start-up:
        # loading it lazily on the first generic prompt took ~20s, as long as the gateway
        # waits for an answer, so that first REST/voice question fell back to an error.
        try:
            await rag_engine.generate_embedding_async("warm-up")
        except Exception as e:
            logger.warning(f"Embedding model warm-up failed: {e}")

        # Keep a reference: asyncio only holds weak references to running tasks.
        self._heartbeat_task = asyncio.create_task(self.emit_heartbeat(redis))
        logger.info(f"🚀 LLM Worker {self.worker_id} started listening on {self.stream_name}...")

        while True:
            try:
                # 1. Crash recovery: this consumer's un-ACKed entries first
                pending_events = await redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.worker_id,
                    streams={self.stream_name: "0"},
                    count=self.max_batch_size
                )
                pending_batch = [(m_id, p) for _s, msgs in (pending_events or []) for m_id, p in msgs]
                if pending_batch:
                    logger.info(f"🔄 Recovered {len(pending_batch)} unacknowledged tasks.")
                    await self.process_batch(pending_batch, redis)
                    continue

                # 2. Read new events up to max_batch_size
                events = await redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.worker_id,
                    streams={self.stream_name: ">"},
                    count=self.max_batch_size,
                    block=self.batch_window_ms
                )
                batch = [(m_id, p) for _s, msgs in (events or []) for m_id, p in msgs]
                if batch:
                    await self.process_batch(batch, redis)

            except Exception as e:
                logger.error(f"LLM Worker loop error: {e}")
                await asyncio.sleep(1)


if __name__ == "__main__":
    worker = LLMWorker()
    asyncio.run(worker.run())
