"""
TTS worker: consumes answer text from ``stream:tts:synthesize`` (group ``tts_group``),
synthesises MP3 audio with edge-tts (Microsoft's online neural voices, so this worker
needs outbound internet access) and publishes it as an ``audio_response`` event on the
session's Pub/Sub channel for the gateway to forward.
"""
import asyncio
import base64
import json
import time
import uuid
import edge_tts
import redis.asyncio as aioredis
from app.config import settings
from app.queue_manager import session_channel
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.tts_worker")

class TTSWorker:
    def __init__(self, voice: str = settings.TTS_VOICE, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"tts-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_TTS_SYNTHESIS
        self.group_name = "tts_group"
        self.voice = voice

        logger.info(f"🔊 Initialized Neural Voice TTS Worker [{self.voice}] (ID: {self.worker_id})...")

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:tts:{self.worker_id}", str(time.time()), ex=10)
            except Exception as e:
                logger.error(f"TTS Heartbeat emission failed: {e}")
            await asyncio.sleep(5)

    async def synthesize_to_bytes(self, text: str) -> bytes:
        """Synthesizes text to MP3 audio bytes using edge-tts."""
        communicate = edge_tts.Communicate(text, self.voice)
        audio_chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])
        return b"".join(audio_chunks)

    async def process_task(self, msg_id: str, payload: dict, redis: aioredis.Redis):
        session_id = payload.get("session_id", "unknown")
        text = payload.get("text", "")
        trace_id = payload.get("trace_id", "unknown")

        start_time = time.time()
        try:
            audio_bytes = await self.synthesize_to_bytes(text)
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
            latency_ms = int((time.time() - start_time) * 1000)

            logger.info(f"🎙️ Synthesized audio for session {session_id} in {latency_ms}ms ({len(audio_bytes)} bytes)")

            # Publish audio stream event to session Pub/Sub channel
            await redis.publish(session_channel(session_id), json.dumps({
                "type": "audio_response",
                "audio_data": audio_b64,
                "latency_ms": latency_ms,
                "trace_id": trace_id
            }))

            await redis.xack(self.stream_name, self.group_name, msg_id)
        except Exception as e:
            # Audio is best-effort: the text answer was already streamed by the LLM worker,
            # so a synthesis failure is logged and ACKed rather than retried.
            logger.error(f"TTS synthesis failed for task {msg_id}: {e}")
            await redis.xack(self.stream_name, self.group_name, msg_id)

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass

        # Keep a reference: asyncio only holds weak references to running tasks.
        self._heartbeat_task = asyncio.create_task(self.emit_heartbeat(redis))
        logger.info(f"🚀 TTS Worker {self.worker_id} started listening on {self.stream_name}...")

        while True:
            try:
                events = await redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.worker_id,
                    streams={self.stream_name: ">"},
                    count=1,
                    block=2000
                )

                if not events:
                    continue

                for _stream_key, messages in events:
                    for msg_id, payload in messages:
                        await self.process_task(msg_id, payload, redis)
            except Exception as e:
                logger.error(f"TTS Worker loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    worker = TTSWorker()
    asyncio.run(worker.run())
