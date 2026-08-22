import asyncio
import time
import redis.asyncio as aioredis
from app.config import settings
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.supervisor")

class WorkerSupervisor:
    def __init__(self, redis_url: str = settings.REDIS_URL):
        self.redis_url = redis_url
        self.streams_to_monitor = [
            (settings.STREAM_STT_AUDIO, "stt_group"),
            (settings.STREAM_LLM_INFERENCE, "llm_group")
        ]
        self.min_idle_time_ms = 15000 # 15s idle before task is considered stranded

    async def scan_and_reclaim_stranded_tasks(self, redis: aioredis.Redis):
        """Scans consumer group pending entries list (PEL) and reclaims stranded tasks using XCLAIM."""
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
                        msg_id = entry["message_id"]
                        idle_time = entry["idle_time"]
                        delivery_count = entry["delivery_count"]
                        
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
                                logger.info(f"♻️ Reclaiming stranded task {msg_id} (Idle: {idle_time}ms)...")
                                await redis.xclaim(
                                    stream_name, group_name, "supervisor", self.min_idle_time_ms, [msg_id]
                                )
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
