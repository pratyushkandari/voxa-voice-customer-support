import asyncio
import re
from unittest.mock import AsyncMock
from app.circuit_breaker import DistributedCircuitBreaker
from workers.worker_supervisor import WorkerSupervisor

def test_circuit_breaker_probe_gating():
    async def _test():
        cb = DistributedCircuitBreaker()
        cb.redis = AsyncMock()
        
        # Test CLOSED state
        cb.redis.get = AsyncMock(return_value="CLOSED")
        allowed, state = await cb.can_execute("ai_worker")
        assert allowed is True
        assert state == "CLOSED"

        # Test OPEN state before recovery timeout
        cb.redis.get = AsyncMock(side_effect=["OPEN", "9999999999"]) # last trip in the future
        allowed, state = await cb.can_execute("ai_worker")
        assert allowed is False
        assert state == "OPEN"

        # Test HALF_OPEN probe gating (first probe acquired lock)
        cb.redis.get = AsyncMock(return_value="HALF_OPEN")
        cb.redis.set = AsyncMock(return_value=True) # lock acquired
        allowed, state = await cb.can_execute("ai_worker")
        assert allowed is True
        assert state == "HALF_OPEN"
        cb.redis.set.assert_called_with("circuit:ai_worker:probe_lock", "1", nx=True, ex=10)

        # Second concurrent probe in HALF_OPEN (lock already taken)
        cb.redis.set = AsyncMock(return_value=False)
        allowed, state = await cb.can_execute("ai_worker")
        assert allowed is False
        assert state == "HALF_OPEN"

    asyncio.run(_test())

def test_worker_supervisor_xpending_keys_and_recovery():
    async def _test():
        supervisor = WorkerSupervisor()
        supervisor.streams_to_monitor = [("stream:audio:transcribe", "stt_group")]
        mock_redis = AsyncMock()
        
        # Mock pending summary
        mock_redis.xpending = AsyncMock(return_value={"pending": 2})
        
        # Mock pending entries returned by redis-py (with time_since_delivered / times_delivered)
        mock_redis.xpending_range = AsyncMock(return_value=[
            {
                "message_id": "1001-0",
                "consumer": "dead_worker_1",
                "time_since_delivered": 20000, # 20s > 15s min_idle
                "times_delivered": 1           # Retries < 3 -> should reclaim and re-enqueue
            },
            {
                "message_id": "1002-0",
                "consumer": "dead_worker_2",
                "time_since_delivered": 25000, # 25s > 15s min_idle
                "times_delivered": 3           # Retries >= 3 -> should route to DLQ
            }
        ])
        
        mock_redis.xclaim = AsyncMock(side_effect=[
            [("stream:audio:transcribe", {"payload": "audio_1"})],
            [("stream:audio:transcribe", {"payload": "poison_audio"})]
        ])
        mock_redis.xadd = AsyncMock()
        mock_redis.xack = AsyncMock()
        
        await supervisor.scan_and_reclaim_stranded_tasks(mock_redis)
        
        xadd_calls = [c.args for c in mock_redis.xadd.call_args_list]

        # Verify 1001-0 was re-enqueued to the same stream and acknowledged
        assert ("stream:audio:transcribe", {
            "payload": "audio_1",
            "retry_count": "2",
            "reclaimed_from_msg_id": "1001-0"
        }) in xadd_calls
        mock_redis.xack.assert_any_call("stream:audio:transcribe", "stt_group", "1001-0")

        # Verify 1002-0 was routed to the DLQ (not re-enqueued) and acknowledged
        dlq_payloads = [payload for stream, payload in xadd_calls if stream == "stream:dead:letter:queue"]
        assert len(dlq_payloads) == 1 and dlq_payloads[0]["payload"] == "poison_audio"
        mock_redis.xack.assert_any_call("stream:audio:transcribe", "stt_group", "1002-0")

        # Every XADD is length-capped so streams cannot grow without bound
        assert all(c.kwargs.get("maxlen") for c in mock_redis.xadd.call_args_list)

    asyncio.run(_test())

def test_word_boundaries_no_false_positives():
    # Test 'sue' doesn't match 'tissue' or 'issue'
    query_tissue = "Can you pass me a tissue for this issue?"
    assert not re.search(r'\b(sue|suing|lawsuit)\b', query_tissue.lower())
    
    query_sue = "I will sue your company unless you refund me"
    assert bool(re.search(r'\b(sue|suing|lawsuit)\b', query_sue.lower()))

    # Test 'vat' doesn't match 'private' or 'elevation'
    query_private = "Is this a private conversation about elevation?"
    assert not re.search(r'\bvat\b', query_private.lower())

    query_vat = "Do you provide VAT invoices for corporate orders?"
    assert bool(re.search(r'\bvat\b', query_vat.lower()))

    # Test 'water' doesn't match 'waterfall'
    query_waterfall = "I like listening to the waterfall audio preset"
    assert not re.search(r'\b(pool|submerged|water|dropped in)\b', query_waterfall.lower())

    query_water = "I accidentally dropped the earbuds in the water pool"
    assert bool(re.search(r'\b(pool|submerged|water|dropped in)\b', query_water.lower()))
