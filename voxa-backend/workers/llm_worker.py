"""
LLM ("reasoning") worker: consumes prompts from ``stream:llm:inference`` (group
``llm_group``), produces an answer and streams it word-by-word to the session's Pub/Sub
channel ``token:stream:{session_id}``. Voice-originated prompts are also forwarded to
the TTS stream.

Answer generation today is deterministic: tool intents (order lookup, refund check,
human escalation) are executed against PostgreSQL, and everything else is answered with
the best-matching knowledge-base passage from pgvector. ``LLM_MODEL_NAME`` names the
generative model this worker is designed to host, but no generative model is loaded yet.
"""
import asyncio
import json
import logging
import time
import uuid
from typing import List, Optional, Tuple

import redis.asyncio as aioredis

from app.agent_tools import detect_tool_intent, execute_tool_call
from app.config import settings
from app.database import AsyncSessionLocal
from app.queue_manager import session_channel
from app.rag_engine import rag_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.llm_worker")

MAX_ATTEMPTS = 3
FALLBACK_POLICY_ANSWER = (
    "Thank you for contacting Voxa support! All purchases include a 30-day "
    "money-back guarantee and 1-year manufacturer warranty. Domestic orders arrive in "
    "3 to 5 business days. Please provide your order ID (e.g. ORD-88412) for live tracking!"
)


def format_tool_response(tool_name: str, tool_result: dict) -> str:
    """Renders a tool result as a customer-facing sentence (handles 'not found' results)."""
    if tool_name in ("lookup_order", "check_refund_eligibility") and not tool_result.get("found"):
        return tool_result.get("message", "I could not find that order. Please double check the ID.")
    if tool_name == "lookup_order":
        return (f"Order {tool_result.get('order_id')} ({tool_result.get('item_name')}) is currently "
                f"{tool_result.get('status')}. Tracking number: {tool_result.get('tracking_number')}.")
    if tool_name == "check_refund_eligibility":
        verdict = "Eligible for full refund" if tool_result.get("eligible") else "Ineligible for refund"
        return f"Order {tool_result.get('order_id')}: {verdict} ({tool_result.get('reason')})."
    return (f"I have escalated your request to a human supervisor (Ticket ID: {tool_result.get('ticket_id')}). "
            f"Priority: {tool_result.get('priority')}.")


class LLMWorker:
    def __init__(self, model_name: str = settings.LLM_MODEL_NAME, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"llm-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_LLM_INFERENCE
        self.group_name = "llm_group"
        self.batch_window_ms = 15  # XREADGROUP block time used to coalesce a micro-batch
        self.max_batch_size = 8
        logger.info(
            f"🧠 LLM worker {self.worker_id} ready (deterministic tool routing + pgvector RAG; "
            f"generative model '{model_name}' is not loaded)."
        )

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:llm:{self.worker_id}", str(time.time()), ex=10)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
            await asyncio.sleep(5)

    async def generate_response(self, prompt: str, user_id: Optional[str]) -> str:
        """Resolves a prompt to an answer via tool routing or knowledge-base retrieval."""
        has_tool, tool_name, tool_args = detect_tool_intent(prompt)
        if has_tool:
            parsed_uid = None
            if user_id and user_id != "guest":
                try:
                    parsed_uid = uuid.UUID(str(user_id))
                except (ValueError, TypeError):
                    parsed_uid = None
            async with AsyncSessionLocal() as db:
                tool_result = await execute_tool_call(tool_name, tool_args, parsed_uid, db)
            return format_tool_response(tool_name, tool_result)

        async with AsyncSessionLocal() as db:
            rag_docs = await rag_engine.search_knowledge_base(prompt, db, limit=2)
        if rag_docs and rag_docs[0].get("similarity", 0) > 0.35:
            return f"Based on Voxa's {rag_docs[0]['topic']} policy: {rag_docs[0]['content']}"
        return FALLBACK_POLICY_ANSWER

    async def process_message(self, msg_id: str, payload: dict, redis: aioredis.Redis):
        """Handles one task end-to-end. Never raises: failures are retried, then DLQ'd."""
        session_id = payload.get("session_id", "unknown")
        trace_id = payload.get("trace_id", "unknown")
        retry_count = int(payload.get("retry_count", 0))
        channel = session_channel(session_id)
        start_time = time.time()

        try:
            response_text = await self.generate_response(payload.get("prompt", ""), payload.get("user_id"))
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
                "trace_id": trace_id
            }))
            await asyncio.sleep(0.03)

        await redis.publish(channel, json.dumps({
            "type": "token_chunk",
            "token": "",
            "is_final": True,
            "ttft_ms": ttft_ms,
            "total_latency_ms": int((time.time() - start_time) * 1000),
            "trace_id": trace_id
        }))

        await redis.xack(self.stream_name, self.group_name, msg_id)

    async def process_batch(self, batch: List[Tuple[str, dict]], redis: aioredis.Redis):
        """Processes a micro-batch (up to max_batch_size tasks) sequentially, in arrival order."""
        logger.info(f"⚡ Processing micro-batch of {len(batch)} items...")
        for msg_id, payload in batch:
            await self.process_message(msg_id, payload, redis)

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass  # BUSYGROUP: group already exists

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
