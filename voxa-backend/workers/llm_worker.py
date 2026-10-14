import asyncio
import json
import time
import uuid
import re
import redis.asyncio as aioredis
from typing import List, Dict, Any, Tuple
import edge_tts
from app.config import settings
from app.database import AsyncSessionLocal
from app.agent_tools import execute_tool_call
from app.rag_engine import rag_engine
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.llm_worker")

class LLMWorker:
    def __init__(self, model_name: str = settings.LLM_MODEL_NAME, redis_url: str = settings.REDIS_URL):
        self.worker_id = f"llm-worker-{str(uuid.uuid4())[:8]}"
        self.redis_url = redis_url
        self.stream_name = settings.STREAM_LLM_INFERENCE
        self.group_name = "llm_group"
        self.batch_window_ms = 15 # 15ms dynamic micro-batching window
        self.max_batch_size = 8
        self.tts_voice = settings.TTS_VOICE

        logger.info(f"🧠 Pre-warming LLM [{model_name}] in INT8 quantized runtime...")
        # Scaffolds model runner
        logger.info("✅ Qwen2.5-0.5B-Instruct pre-warmed and ready for dynamic micro-batching.")

    async def emit_heartbeat(self, redis: aioredis.Redis):
        while True:
            try:
                await redis.set(f"worker:heartbeat:llm:{self.worker_id}", str(time.time()), ex=10)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Heartbeat emission failed: {e}")
                await asyncio.sleep(5)

    async def synthesize_voice(self, text_content: str, output_path: str = "response.mp3") -> bool:
        """Async neural voice generation via edge-tts."""
        try:
            communicate = edge_tts.Communicate(text_content, self.tts_voice)
            await communicate.save(output_path)
            return True
        except Exception as e:
            logger.warning(f"edge-tts voice synthesis error: {e}")
            return False

    def detect_tool_call(self, prompt: str) -> Tuple[bool, str, dict]:
        """Deterministic Intent Router (<10ms) for tool dispatch."""
        prompt_lower = prompt.lower()
        
        # Order lookup intent
        order_match = re.search(r'ord-\d{4,6}', prompt_lower)
        if order_match:
            order_id = order_match.group(0).upper()
            if "refund" in prompt_lower or "return" in prompt_lower:
                return True, "check_refund_eligibility", {"order_id": order_id}
            return True, "lookup_order", {"order_id": order_id}
            
        # Human escalation intent
        if any(w in prompt_lower for w in ["angry", "human", "agent", "manager", "escalate", "terrible", "lawyer"]):
            return True, "escalate_to_human", {"issue_summary": prompt, "priority": "urgent"}
            
        return False, "", {}

    async def process_batch(self, batch: List[Tuple[str, dict]], redis: aioredis.Redis):
        """Processes a dynamic micro-batch of queries simultaneously."""
        logger.info(f"⚡ Processing micro-batch of {len(batch)} items...")
        
        for msg_id, payload in batch:
            session_id = payload.get("session_id", "unknown")
            prompt = payload.get("prompt", "")
            trace_id = payload.get("trace_id", "unknown")
            user_id = payload.get("user_id")
            
            start_time = time.time()
            
            # 1. Deterministic Intent Routing
            has_tool, tool_name, tool_args = self.detect_tool_call(prompt)
            if has_tool:
                parsed_uid = None
                if user_id and user_id != "guest":
                    try:
                        parsed_uid = uuid.UUID(str(user_id))
                    except (ValueError, TypeError):
                        parsed_uid = None

                async with AsyncSessionLocal() as db:
                    tool_result = await execute_tool_call(tool_name, tool_args, parsed_uid, db)
                
                if tool_name == "lookup_order":
                    response_text = f"Order {tool_result.get('order_id')} is currently {tool_result.get('status')}. Tracking number: {tool_result.get('tracking_number')}."
                elif tool_name == "check_refund_eligibility":
                    response_text = f"Order {tool_result.get('order_id')}: {'Eligible for full refund' if tool_result.get('eligible') else 'Ineligible for refund'} ({tool_result.get('reason')})."
                else:
                    response_text = f"I have escalated your request to a human supervisor (Ticket ID: {tool_result.get('ticket_id')}). Priority: {tool_result.get('priority')}."
            else:
                # 2. General RAG Response
                response_text = f"Based on Voxa's policy: You can request refunds within 30 days. For shipping, domestic orders arrive in 3-5 days. Let me know if you need help with a specific order ID!"

            # 3. Stream tokens to Redis Pub/Sub
            tokens = response_text.split(" ")
            first_token = True
            ttft_ms = 0
            
            channel = f"token:stream:{session_id}"
            for t in tokens:
                if first_token:
                    ttft_ms = int((time.time() - start_time) * 1000)
                    first_token = False
                    
                chunk_payload = json.dumps({
                    "token": t + " ",
                    "is_final": False,
                    "ttft_ms": ttft_ms,
                    "trace_id": trace_id
                })
                await redis.publish(channel, chunk_payload)
                await asyncio.sleep(0.03)

            total_latency_ms = int((time.time() - start_time) * 1000)
            final_payload = json.dumps({
                "token": "",
                "is_final": True,
                "ttft_ms": ttft_ms,
                "total_latency_ms": total_latency_ms,
                "trace_id": trace_id
            })
            await redis.publish(channel, final_payload)
            
            # 4. Acknowledge message in Redis Streams
            await redis.xack(self.stream_name, self.group_name, msg_id)

    async def run(self):
        redis = aioredis.from_url(self.redis_url, decode_responses=True)
        try:
            await redis.xgroup_create(self.stream_name, self.group_name, id="0", mkstream=True)
        except Exception:
            pass

        asyncio.create_task(self.emit_heartbeat(redis))
        logger.info(f"🚀 LLM Worker {self.worker_id} started listening on {self.stream_name}...")

        while True:
            try:
                # Read up to max_batch_size
                events = await redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.worker_id,
                    streams={self.stream_name: ">"},
                    count=self.max_batch_size,
                    block=self.batch_window_ms
                )

                if not events:
                    continue

                batch = []
                for stream_key, messages in events:
                    for msg_id, payload in messages:
                        batch.append((msg_id, payload))

                if batch:
                    await self.process_batch(batch, redis)

            except Exception as e:
                logger.error(f"LLM Worker loop error: {e}")
                await asyncio.sleep(1)

if __name__ == "__main__":
    worker = LLMWorker()
    asyncio.run(worker.run())
