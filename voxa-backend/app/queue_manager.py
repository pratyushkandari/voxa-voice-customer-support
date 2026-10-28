"""
Gateway-side producer for the Redis Streams worker pipeline.

    gateway --XADD--> stream:audio:transcribe --(stt_group)--> STT worker
    STT worker --XADD--> stream:llm:inference --(llm_group)--> LLM worker
    LLM worker --XADD--> stream:tts:synthesize --(tts_group)--> TTS worker
    workers --PUBLISH--> token:stream:{session_id} --> gateway --> WebSocket client

All stream fields are strings (Redis Streams store flat string maps).
"""
import asyncio
import logging
import time
from typing import Iterable, Optional

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from app.config import settings

logger = logging.getLogger("voxa.queue")


def session_channel(session_id: str) -> str:
    """Pub/Sub channel on which workers publish events for one client session."""
    return f"token:stream:{session_id}"


# Must stay well below worker_supervisor's 15s idle threshold.
CLAIM_REFRESH_SEC = 5.0


async def keep_claimed(redis: aioredis.Redis, stream: str, group: str, consumer: str,
                       msg_ids: Iterable[str], interval: Optional[float] = None) -> None:
    """
    Runs until cancelled, re-claiming ``msg_ids`` for ``consumer`` every few seconds.

    The supervisor treats any pending entry idle for more than 15s as stranded by a
    crashed worker and re-enqueues it. A healthy worker that is still busy (a long Whisper
    transcription, or messages waiting in a micro-batch) would otherwise have its work
    duplicated. ``XCLAIM ... JUSTID`` resets the idle time without bumping the delivery
    counter. Errors are logged and retried on the next tick.
    """
    ids = list(msg_ids)
    while True:
        await asyncio.sleep(CLAIM_REFRESH_SEC if interval is None else interval)
        try:
            await redis.xclaim(stream, group, consumer, 0, ids, justid=True)
        except Exception as e:
            logger.warning(f"Could not refresh claim on {len(ids)} entries of {stream}: {e}")


class QueueManager:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    async def init_consumer_groups(self):
        """Idempotently creates the worker consumer groups (and their streams)."""
        streams = [
            (settings.STREAM_STT_AUDIO, "stt_group"),
            (settings.STREAM_LLM_INFERENCE, "llm_group"),
            (settings.STREAM_TTS_SYNTHESIS, "tts_group"),
            (settings.STREAM_DLQ, "dlq_group")
        ]
        for stream, group in streams:
            try:
                await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
                logger.info(f"Created consumer group {group} on stream {stream}")
            except ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise  # anything other than "group already exists" is a real error

    async def publish_stt_task(self, session_id: str, audio_bytes_b64: str, trace_id: str,
                               user_id: Optional[str] = None) -> str:
        """Pushes a base64-encoded audio utterance to the STT stream."""
        payload = {
            "session_id": session_id,
            "user_id": user_id or "guest",
            "audio_data": audio_bytes_b64,
            "trace_id": trace_id,
            "timestamp": str(time.time()),
            "retry_count": "0"
        }
        return await self.redis.xadd(
            settings.STREAM_STT_AUDIO, payload, maxlen=settings.STREAM_MAXLEN, approximate=True
        )


queue_manager = QueueManager()
