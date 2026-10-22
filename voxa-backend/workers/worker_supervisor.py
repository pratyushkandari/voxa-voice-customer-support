import asyncio
import time
import redis.asyncio as aioredis
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
      3. Guards against poison-pill execution loops: tasks exceeding max delivery retries (>= 3)
         are quarantined to the Dead-Letter Queue (STREAM_DLQ) with diagnostic metadata.
      4. Normalizes redis-py telemetry schemas (time_since_delivered / idle_time).
    """
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis_url = redis_url
        self.streams_to_monitor = [
            (settings.STREAM_STT_AUDIO, "stt_group"),
            (settings.STREAM_LLM_INFERENCE, "llm_group")
        ]
        self.min_idle_time_ms = 15000  # 15s idle threshold before task is considered stranded

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
                    
                    # Fetch detailed pending entries
                    pending_entries = await redis.xpending_range(
                        stream_name, group_name, min="-", max="+", count=10
                    )
                    
                    for entry in pending_entries:
                        msg_id = entry.get("message_id")
                        idle_time = entry.get("time_since_delivered", entry.get("idle_time", 0))
                        delivery_count = entry.get("times_delivered", entry.get("delivery_count", 0))
                        
                        if idle_time >= self.min_idle_time_ms:
                            if delivery_count >= 3:
                                logger.warning(f"☠️ Poison payload detected [{msg_id}]. Moving to DLQ...")
                                # Fetch message content and route to DLQ
                                msg_data = await redis.xclaim(
                                    stream_name, group_name, "supervisor", self.min_idle_time_ms, [msg_id]
                                )
                                if msg_data:
                                    payload = msg_data[0][1]
                                    await redis.xadd(settings.STREAM_DLQ, {
                                        **payload,
                                        "origin_stream": stream_name,
                                        "reason": "Exceeded maximum delivery retries (3)"
                                    })
                                    await redis.xack(stream_name, group_name, msg_id)
                            else:
                                logger.info(f"♻️ Reclaiming stranded task {msg_id} (Idle: {idle_time}ms, Retries: {delivery_count})...")
                                # Claim stranded message, increment retry count, and re-enqueue for active worker consumption
                                msg_data = await redis.xclaim(
                                    stream_name, group_name, "supervisor", self.min_idle_time_ms, [msg_id]
                                )
                                if msg_data:
                                    payload = msg_data[0][1]
                                    payload["retry_count"] = str(delivery_count + 1)
                                    payload["reclaimed_from_msg_id"] = str(msg_id)
                                    # Push back to stream head so any active worker picks it up immediately
                                    await redis.xadd(stream_name, payload)
                                    await redis.xack(stream_name, group_name, msg_id)
                                    logger.info(f"✅ Successfully re-enqueued stranded task {msg_id} for active workers.")
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
