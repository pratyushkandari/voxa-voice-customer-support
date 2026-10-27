import asyncio
import redis.asyncio as aioredis
from redis.exceptions import ResponseError
from app.config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.supervisor")

class WorkerSupervisor:
    """
    Distributed Worker Supervisor and Stream Reliability Daemon.

    Monitors Redis Streams Pending Entries Lists (PEL) across asynchronous worker consumer groups.
    Guarantees at-least-once processing semantics:
      1. Detects tasks stranded by abrupt worker process termination (SIGKILL / OOM / crash).
      2. Reclaims stranded tasks using atomic XCLAIM if unacknowledged beyond idle timeout.
      3. Guards against poison-pill execution loops: tasks that have already been attempted
         ``max_attempts`` (3) times are quarantined to the Dead-Letter Queue (STREAM_DLQ).
      4. Normalizes redis-py telemetry schemas (time_since_delivered / idle_time).
    """
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis_url = redis_url
        self.streams_to_monitor = [
            (settings.STREAM_STT_AUDIO, "stt_group"),
            (settings.STREAM_LLM_INFERENCE, "llm_group"),
            (settings.STREAM_TTS_SYNTHESIS, "tts_group"),
        ]
        self.min_idle_time_ms = 15000  # 15s idle threshold before task is considered stranded
        self.max_attempts = 3

    async def scan_and_reclaim_stranded_tasks(self, redis: aioredis.Redis):
        """
        Scans consumer group Pending Entries List (PEL) and reclaims stranded or timed-out tasks.
        Safely maps redis-py pending range responses to prevent KeyError exceptions.
        """
        for stream_name, group_name in self.streams_to_monitor:
            try:
                # Check pending list
                pending_summary = await redis.xpending(stream_name, group_name)
                pending_count = pending_summary.get("pending", 0)
                
                if pending_count > 0:
                    logger.info(f"🔍 Found {pending_count} pending messages in {stream_name} ({group_name})")
                    
                    # Only entries idle past the threshold (Redis >= 6.2 IDLE filter), so
                    # busy-but-healthy entries at the head of the PEL cannot hide stranded ones.
                    pending_entries = await redis.xpending_range(
                        stream_name, group_name, min="-", max="+", count=10,
                        idle=self.min_idle_time_ms,
                    )
                    
                    for entry in pending_entries:
                        msg_id = entry.get("message_id")
                        idle_time = entry.get("time_since_delivered", entry.get("idle_time", 0))
                        delivery_count = entry.get("times_delivered", entry.get("delivery_count", 0))
                        
                        if idle_time < self.min_idle_time_ms:
                            continue

                        # Claim first (atomic: only one supervisor wins if several run) so we
                        # can read the payload and its cumulative retry_count.
                        msg_data = await redis.xclaim(
                            stream_name, group_name, "supervisor", self.min_idle_time_ms, [msg_id]
                        )
                        if not msg_data or not msg_data[0][1]:
                            continue  # already claimed elsewhere, or trimmed from the stream
                        payload = dict(msg_data[0][1])

                        # Re-enqueueing creates a NEW stream entry whose delivery counter
                        # starts at 1 again, so the attempt count must be carried in the
                        # payload. Otherwise a payload that crashes every worker would be
                        # recycled forever instead of reaching the DLQ.
                        attempts = max(int(payload.get("retry_count", 0) or 0), int(delivery_count)) + 1

                        if attempts > self.max_attempts:
                            logger.warning(f"☠️ Poison payload detected [{msg_id}]. Moving to DLQ...")
                            await redis.xadd(settings.STREAM_DLQ, {
                                **payload,
                                "origin_stream": stream_name,
                                "reason": f"Exceeded maximum delivery attempts ({self.max_attempts})"
                            }, maxlen=settings.STREAM_MAXLEN, approximate=True)
                        else:
                            logger.info(f"♻️ Reclaiming stranded task {msg_id} (Idle: {idle_time}ms, Attempt: {attempts})...")
                            payload["retry_count"] = str(attempts)
                            payload["reclaimed_from_msg_id"] = str(msg_id)
                            # Re-append so any live consumer in the group picks it up.
                            await redis.xadd(stream_name, payload, maxlen=settings.STREAM_MAXLEN, approximate=True)
                            logger.info(f"✅ Successfully re-enqueued stranded task {msg_id} for active workers.")
                        await redis.xack(stream_name, group_name, msg_id)
            except ResponseError as e:
                if "NOGROUP" in str(e):
                    # Stream/group not created yet (the gateway creates them on startup).
                    logger.debug(f"{stream_name} ({group_name}) does not exist yet")
                else:
                    logger.error(f"Supervisor error scanning {stream_name}: {e}")
            except Exception as e:
                logger.error(f"Supervisor error scanning {stream_name}: {e}")

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        logger.info("🛡️ Worker Supervisor Daemon active. Monitoring PEL and heartbeats...")
        
        while True:
            await self.scan_and_reclaim_stranded_tasks(redis)
            await asyncio.sleep(5)

if __name__ == "__main__":
    supervisor = WorkerSupervisor()
    asyncio.run(supervisor.run())
