"""
STT worker: consumes base64 audio from ``stream:audio:transcribe`` (group ``stt_group``),
transcribes it with Faster-Whisper (CTranslate2, INT8 on CPU) and forwards the text to
``stream:llm:inference`` for the LLM worker.

Failure handling: a failed task is re-enqueued with ``retry_count + 1``; after 3 attempts
it goes to the dead-letter queue. Messages stranded by a crashed worker are re-delivered
by ``worker_supervisor``.
"""
import asyncio
import base64
import json
import logging
import os
import tempfile
import time
import uuid

import redis.asyncio as aioredis
from faster_whisper import WhisperModel

from app.config import settings
from app.queue_manager import session_channel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.stt_worker")

MAX_ATTEMPTS = 3


class STTWorker:
    def __init__(self, model_size: str = settings.STT_MODEL_SIZE, redis_url: str = settings.REDIS_URL,
                 load_model: bool = True):
        self.worker_id = f"stt-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_STT_AUDIO
        self.group_name = "stt_group"
        self.model = None

        if load_model:
            # No silent fallback: if the model cannot load, the worker must crash so the
            # orchestrator restarts it and the failure is visible, instead of answering
            # every utterance with fabricated text.
            logger.info(f"🎙️ Loading Faster-Whisper [{model_size}] with INT8 quantization...")
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("✅ Faster-Whisper loaded.")

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:stt:{self.worker_id}", str(time.time()), ex=10)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
            await asyncio.sleep(5)

    def transcribe_audio_bytes(self, audio_bytes: bytes) -> str:
        """
        Blocking Whisper inference. Faster-Whisper decodes via FFmpeg/PyAV, which sniffs
        the container from the bytes, so WAV, WebM/Opus or OGG uploads all work.
        """
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_path = temp_audio.name
            temp_audio.write(audio_bytes)

        try:
            segments, _info = self.model.transcribe(temp_path, beam_size=2)
            return " ".join(segment.text for segment in segments).strip()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    async def handle_message(self, redis: aioredis.Redis, msg_id: str, payload: dict):
        """Processes one STT task; always ends with the message ACKed (done, retried or DLQ'd)."""
        session_id = payload.get("session_id", "unknown")
        trace_id = payload.get("trace_id", "unknown")
        retry_count = int(payload.get("retry_count", 0))
        start_time = time.time()
        try:
            audio_bytes = base64.b64decode(payload.get("audio_data", ""), validate=True)
            # Inference is CPU-bound: run it off the event loop so heartbeats keep flowing.
            text_result = await asyncio.to_thread(self.transcribe_audio_bytes, audio_bytes)
            stt_latency_ms = int((time.time() - start_time) * 1000)
            logger.info(f"🗣️ Transcribed [Session: {session_id}] ({stt_latency_ms}ms): '{text_result}'")

            if not text_result:
                await redis.publish(session_channel(session_id), json.dumps({
                    "type": "error",
                    "message": "No speech was detected in the recording. Please try again.",
                    "trace_id": trace_id,
                }))
            else:
                # Forward transcribed query to the LLM stream with voice-pipeline flags
                await redis.xadd(settings.STREAM_LLM_INFERENCE, {
                    "session_id": session_id,
                    "user_id": payload.get("user_id", "guest"),
                    "prompt": text_result,
                    "conversation_id": payload.get("conversation_id", ""),
                    "trace_id": trace_id,
                    "stt_latency_ms": str(stt_latency_ms),
                    "origin": "audio",
                    "needs_audio": "true",
                    "retry_count": "0"
                }, maxlen=settings.STREAM_MAXLEN, approximate=True)

            await redis.xack(self.stream_name, self.group_name, msg_id)

        except Exception as ex:
            logger.error(f"Error processing audio task {msg_id} (attempt {retry_count + 1}): {ex}")
            if retry_count + 1 >= MAX_ATTEMPTS:
                await redis.xadd(settings.STREAM_DLQ, {
                    **payload,
                    "origin_stream": self.stream_name,
                    "error": str(ex),
                    "failed_worker": self.worker_id
                }, maxlen=settings.STREAM_MAXLEN, approximate=True)
                await redis.publish(session_channel(session_id), json.dumps({
                    "type": "error",
                    "message": "Sorry, we could not process that recording.",
                    "trace_id": trace_id,
                }))
            else:
                await redis.xadd(self.stream_name, {**payload, "retry_count": str(retry_count + 1)},
                                 maxlen=settings.STREAM_MAXLEN, approximate=True)
            await redis.xack(self.stream_name, self.group_name, msg_id)

    async def read_next_batch(self, redis: aioredis.Redis):
        """
        Returns [(msg_id, payload), ...]: first this consumer's own un-ACKed entries
        (crash recovery), otherwise new entries (blocking up to 2s).

        Reading history with ID "0" returns ``[[stream, []]]`` when nothing is pending;
        that non-empty-but-empty reply must be treated as "no pending work", or the
        worker would spin forever and never read new messages.
        """
        pending = await redis.xreadgroup(
            groupname=self.group_name, consumername=self.worker_id,
            streams={self.stream_name: "0"}, count=1
        )
        batch = [(msg_id, payload) for _stream, messages in (pending or []) for msg_id, payload in messages]
        if batch:
            return batch

        fresh = await redis.xreadgroup(
            groupname=self.group_name, consumername=self.worker_id,
            streams={self.stream_name: ">"}, count=1, block=2000
        )
        return [(msg_id, payload) for _stream, messages in (fresh or []) for msg_id, payload in messages]

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass  # BUSYGROUP: group already exists

        # Keep a reference: asyncio only holds weak references to running tasks.
        self._heartbeat_task = asyncio.create_task(self.emit_heartbeat(redis))
        logger.info(f"🚀 STT Worker {self.worker_id} started listening on {self.stream_name}...")

        while True:
            try:
                for msg_id, payload in await self.read_next_batch(redis):
                    await self.handle_message(redis, msg_id, payload)
            except Exception as e:
                logger.error(f"STT Worker loop error: {e}")
                await asyncio.sleep(1)


if __name__ == "__main__":
    worker = STTWorker()
    asyncio.run(worker.run())
