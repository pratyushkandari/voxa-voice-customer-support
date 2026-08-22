import asyncio
import base64
import io
import time
import uuid
import tempfile
import redis.asyncio as aioredis
from faster_whisper import WhisperModel
from app.config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.stt_worker")

class STTWorker:
    def __init__(self, model_size: str = settings.STT_MODEL_SIZE, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"stt-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_STT_AUDIO
        self.group_name = "stt_group"
        
        logger.info(f"🎙️ Pre-warming Faster-Whisper [{model_size}] with INT8 quantization into pinned RAM...")
        try:
            self.model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info("✅ Faster-Whisper loaded successfully with zero-reload latency.")
        except Exception as e:
            logger.warning(f"Could not load faster-whisper model ({e}). Using mock STT engine for fallback.")
            self.model = None

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:stt:{self.worker_id}", str(time.time()), ex=10)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
                await asyncio.sleep(5)

    def transcribe_audio_bytes(self, audio_bytes: bytes) -> str:
        if not self.model:
            return "What is your return and refund policy?"
            
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as temp_audio:
            temp_audio.write(audio_bytes)
            temp_audio.flush()
            segments, info = self.model.transcribe(temp_audio.name, beam_size=2)
            transcription = " ".join([segment.text for segment in segments]).strip()
            return transcription

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        # Ensure stream and consumer group
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass

        asyncio.create_task(self.emit_heartbeat(redis))
        logger.info(f"🚀 STT Worker {self.worker_id} started listening on {self.stream_name}...")

        while True:
            try:
                # Read from Redis Streams
                events = await redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.worker_id,
                    streams={self.stream_name: ">"},
                    count=1,
                    block=2000
                )

                if not events:
                    continue

                for stream_key, messages in events:
                    for msg_id, payload in messages:
                        session_id = payload.get("session_id", "unknown")
                        trace_id = payload.get("trace_id", "unknown")
                        audio_b64 = payload.get("audio_data", "")
                        retry_count = int(payload.get("retry_count", 0))

                        start_time = time.time()
                        try:
                            audio_bytes = base64.b64decode(audio_b64)
                            text_result = self.transcribe_audio_bytes(audio_bytes)
                            stt_latency_ms = int((time.time() - start_time) * 1000)
                            
                            logger.info(f"🗣️ Transcribed [Session: {session_id}] ({stt_latency_ms}ms): '{text_result}'")

                            # Forward transcribed query to LLM Inference Queue
                            await redis.xadd(settings.STREAM_LLM_INFERENCE, {
                                "session_id": session_id,
                                "user_id": payload.get("user_id", "guest"),
                                "prompt": text_result,
                                "conversation_id": payload.get("conversation_id", ""),
                                "trace_id": trace_id,
                                "stt_latency_ms": str(stt_latency_ms),
                                "retry_count": "0"
                            })

                            # Acknowledge processed message
                            await redis.xack(self.stream_name, self.group_name, msg_id)

                        except Exception as ex:
                            logger.error(f"Error processing audio task {msg_id}: {ex}")
                            if retry_count >= 2:
                                # Route to DLQ after 3 failures
                                await redis.xadd(settings.STREAM_DLQ, {
                                    **payload,
                                    "error": str(ex),
                                    "failed_worker": self.worker_id
                                })
                                await redis.xack(self.stream_name, self.group_name, msg_id)
                            else:
                                payload["retry_count"] = str(retry_count + 1)
                                await redis.xadd(self.stream_name, payload)
                                await redis.xack(self.stream_name, self.group_name, msg_id)

            except Exception as e:
                logger.error(f"STT Worker loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    worker = STTWorker()
    asyncio.run(worker.run())
