"""
LLM worker: consumes voice transcripts from ``stream:llm:inference`` (group
``llm_group``), answers them and streams the reply to the session's Pub/Sub channel
``token:stream:{session_id}``, then hands the full reply to the TTS stream.

Answers come from ``app.llm.answer_query``, the same path the gateway uses for typed
questions: the intent router picks the facts and Gemini writes the reply, with the
router's plain reply as the fallback.

Prometheus metrics (Gemini outcome and latency, cache hits) are served on port 9100.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import List, Optional, Tuple

import redis.asyncio as aioredis
from prometheus_client import start_http_server

from app.config import settings
from app.llm import Answer, Send, answer_query
from app.queue_manager import keep_claimed, session_channel
from app.rag_engine import rag_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.llm_worker")

MAX_ATTEMPTS = 3
METRICS_PORT = 9100


class LLMWorker:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"llm-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_LLM_INFERENCE
        self.group_name = "llm_group"
        self.batch_window_ms = 15  # XREADGROUP block time used to coalesce a micro-batch
        self.max_batch_size = 8
        logger.info(f"LLM worker {self.worker_id} initialised (model {settings.GEMINI_MODEL})")

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:llm:{self.worker_id}", str(time.time()), ex=10)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
            await asyncio.sleep(5)

    async def generate_response(self, prompt: str, user_id: Optional[str], send: Send) -> Answer:
        """
        Answers an already-sanitised transcript (the STT worker sanitises it) and streams
        the reply through ``send``.
        """
        parsed_uid = None
        if user_id and user_id != "guest":
            try:
                parsed_uid = uuid.UUID(str(user_id))
            except (ValueError, TypeError):
                parsed_uid = None
        return await answer_query(prompt, prompt, parsed_uid, send)

    async def process_message(self, msg_id: str, payload: dict, redis: aioredis.Redis):
        """Handles one task end-to-end. Never raises: failures are retried, then DLQ'd."""
        session_id = payload.get("session_id", "unknown")
        trace_id = payload.get("trace_id", "unknown")
        retry_count = int(payload.get("retry_count", 0))
        channel = session_channel(session_id)
        start_time = time.time()
        first_token_ms: list[int] = []

        async def publish_token(token: str):
            if not first_token_ms:
                first_token_ms.append(int((time.time() - start_time) * 1000))
            await redis.publish(channel, json.dumps({
                "type": "token_chunk",
                "token": token,
                "is_final": False,
                "ttft_ms": first_token_ms[0],
                "trace_id": trace_id
            }))

        try:
            answer = await self.generate_response(payload.get("prompt", ""), payload.get("user_id"), publish_token)
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

        await redis.publish(channel, json.dumps({
            "type": "token_chunk",
            "token": "",
            "is_final": True,
            "ttft_ms": first_token_ms[0] if first_token_ms else None,
            "total_latency_ms": int((time.time() - start_time) * 1000),
            "cache_hit": answer.cache_hit,
            "fallback": answer.fallback,
            "circuit_breaker": answer.breaker,
            "trace_id": trace_id
        }))

        # Voice pipeline: ask the TTS worker to speak the reply
        if payload.get("needs_audio") == "true":
            try:
                await redis.xadd(settings.STREAM_TTS_SYNTHESIS, {
                    "session_id": session_id,
                    "text": answer.text,
                    "trace_id": trace_id
                }, maxlen=settings.STREAM_MAXLEN, approximate=True)
            except Exception as tts_err:
                logger.warning(f"Could not enqueue TTS synthesis task: {tts_err}")

        await redis.xack(self.stream_name, self.group_name, msg_id)

    async def process_batch(self, batch: List[Tuple[str, dict]], redis: aioredis.Redis):
        """
        Answers a micro-batch (up to max_batch_size tasks) concurrently: each answer mostly
        waits on Gemini, so answering them one by one would make the last caller wait for
        all the others. The entries are kept claimed while they run so the supervisor does
        not mistake them for stranded work and enqueue duplicates.
        """
        logger.info(f"Processing batch of {len(batch)} tasks")
        claim_task = asyncio.create_task(
            keep_claimed(redis, self.stream_name, self.group_name, self.worker_id, [m for m, _ in batch])
        )
        try:
            await asyncio.gather(*(self.process_message(msg_id, payload, redis) for msg_id, payload in batch))
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
        logger.info(f"LLM worker {self.worker_id} started listening on {self.stream_name}")

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
                    logger.info(f"Recovered {len(pending_batch)} unacknowledged tasks")
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
    start_http_server(METRICS_PORT)
    worker = LLMWorker()
    asyncio.run(worker.run())
