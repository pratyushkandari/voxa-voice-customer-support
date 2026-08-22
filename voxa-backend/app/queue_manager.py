import json
import uuid
import time
from typing import Optional, Dict, Any, List
import redis.asyncio as aioredis
from app.config import settings
import logging

logger = logging.getLogger("voxa.queue")

class QueueManager:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis = aioredis.from_url(redis_url, decode_responses=True)

    async def init_consumer_groups(self):
        """Initializes consumer groups for Redis Streams if they don't exist."""
        streams = [
            (settings.STREAM_STT_AUDIO, "stt_group"),
            (settings.STREAM_LLM_INFERENCE, "llm_group"),
            (settings.STREAM_DLQ, "dlq_group")
        ]
        for stream, group in streams:
            try:
                await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
                logger.info(f"Created consumer group {group} on stream {stream}")
            except Exception as e:
                # Group already exists
                pass

    async def publish_stt_task(self, session_id: str, audio_bytes_b64: str, trace_id: str) -> str:
        """Pushes an audio chunk to stream:audio:transcribe."""
        payload = {
            "session_id": session_id,
            "audio_data": audio_bytes_b64,
            "trace_id": trace_id,
            "timestamp": str(time.time()),
            "retry_count": "0"
        }
        msg_id = await self.redis.xadd(settings.STREAM_STT_AUDIO, payload)
        return msg_id

    async def publish_llm_task(self, session_id: str, user_id: str, prompt: str, conversation_id: str, trace_id: str) -> str:
        """Pushes a user inference query to stream:llm:inference."""
        payload = {
            "session_id": session_id,
            "user_id": user_id,
            "prompt": prompt,
            "conversation_id": conversation_id,
            "trace_id": trace_id,
            "timestamp": str(time.time()),
            "retry_count": "0"
        }
        msg_id = await self.redis.xadd(settings.STREAM_LLM_INFERENCE, payload)
        return msg_id

    async def publish_token_chunk(self, session_id: str, token: str, is_final: bool = False, ttft_ms: Optional[int] = None, total_latency_ms: Optional[int] = None):
        """Publishes a generated token chunk to Redis Pub/Sub for immediate WebSocket consumption."""
        channel = f"token:stream:{session_id}"
        message = json.dumps({
            "token": token,
            "is_final": is_final,
            "ttft_ms": ttft_ms,
            "total_latency_ms": total_latency_ms
        })
        await self.redis.publish(channel, message)

    async def move_to_dlq(self, origin_stream: str, task_data: dict, error_message: str):
        """Routes a poison payload to the Dead-Letter Queue (DLQ)."""
        payload = {
            **task_data,
            "origin_stream": origin_stream,
            "error": error_message,
            "failed_at": str(time.time())
        }
        await self.redis.xadd(settings.STREAM_DLQ, payload)
        logger.warning(f"🚨 Task moved to DLQ ({settings.STREAM_DLQ}): {error_message}")

    async def update_worker_heartbeat(self, worker_id: str, worker_type: str):
        """Emits a worker heartbeat every 5 seconds."""
        key = f"worker:heartbeat:{worker_type}:{worker_id}"
        await self.redis.set(key, str(time.time()), ex=10) # 10s TTL

queue_manager = QueueManager()
