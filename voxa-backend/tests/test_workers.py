"""Worker behaviour with Redis replaced by AsyncMock: TTS, retries, the success path and the queue producer."""
import asyncio
import base64
import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ResponseError

import workers.tts_worker as tts_module
from app.config import settings
from app.llm import Answer
from app.queue_manager import QueueManager
from workers.llm_worker import LLMWorker
from workers.stt_worker import STTWorker
from workers.tts_worker import TTSWorker
from workers.worker_supervisor import WorkerSupervisor


# ----------------------------- TTS worker -----------------------------

def test_tts_worker_publishes_the_spoken_reply_and_acks():
    async def _test():
        worker = TTSWorker()
        worker.synthesize_to_bytes = AsyncMock(return_value=b"ID3-mp3-bytes")
        redis = AsyncMock()

        await worker.process_task("5-0", {"session_id": "s1", "text": "It has shipped.", "trace_id": "v1"}, redis)

        channel, data = redis.publish.await_args.args
        event = json.loads(data)
        assert channel == "token:stream:s1" and event["type"] == "audio_response" and event["trace_id"] == "v1"
        assert base64.b64decode(event["audio_data"]) == b"ID3-mp3-bytes"
        worker.synthesize_to_bytes.assert_awaited_once_with("It has shipped.")
        redis.xack.assert_awaited_once_with(settings.STREAM_TTS_SYNTHESIS, "tts_group", "5-0")

    asyncio.run(_test())


def test_tts_failure_is_acked_without_audio():
    """The text reply has already been sent, so a synthesis failure is not retried."""
    async def _test():
        worker = TTSWorker()
        worker.synthesize_to_bytes = AsyncMock(side_effect=ConnectionError("edge-tts unreachable"))
        redis = AsyncMock()

        await worker.process_task("5-0", {"session_id": "s1", "text": "Hi", "trace_id": "v1"}, redis)

        redis.publish.assert_not_awaited()
        redis.xack.assert_awaited_once_with(settings.STREAM_TTS_SYNTHESIS, "tts_group", "5-0")

    asyncio.run(_test())


def test_tts_worker_joins_the_audio_chunks_from_edge_tts(monkeypatch):
    class FakeCommunicate:
        def __init__(self, text, voice):
            self.args = (text, voice)
            FakeCommunicate.last = self

        async def stream(self):
            for chunk in ({"type": "audio", "data": b"ab"}, {"type": "WordBoundary"}, {"type": "audio", "data": b"cd"}):
                yield chunk

    monkeypatch.setattr(tts_module.edge_tts, "Communicate", FakeCommunicate, raising=False)

    audio = asyncio.run(TTSWorker(voice="en-IN-NeerjaNeural").synthesize_to_bytes("Hello"))

    assert audio == b"abcd" and FakeCommunicate.last.args == ("Hello", "en-IN-NeerjaNeural")


# ----------------------------- STT and LLM workers -----------------------------

def test_stt_worker_retries_then_dead_letters_a_recording_it_cannot_decode():
    async def _test():
        worker = STTWorker(load_model=False)

        def broken(audio):
            raise RuntimeError("ffmpeg: invalid data found when processing input")

        worker.transcribe_audio_bytes = broken
        payload = {"session_id": "s1", "audio_data": "AAAA", "trace_id": "v1"}

        redis = AsyncMock()
        await worker.handle_message(redis, "1-0", {**payload, "retry_count": "0"})
        assert redis.xadd.await_args.args[0] == settings.STREAM_STT_AUDIO
        assert redis.xadd.await_args.args[1]["retry_count"] == "1"
        redis.publish.assert_not_awaited()
        redis.xack.assert_awaited_once()

        redis = AsyncMock()
        await worker.handle_message(redis, "2-0", {**payload, "retry_count": "2"})  # third attempt
        dead = redis.xadd.await_args.args
        assert dead[0] == settings.STREAM_DLQ and dead[1]["origin_stream"] == settings.STREAM_STT_AUDIO
        event = json.loads(redis.publish.await_args.args[1])
        assert event["type"] == "error" and "could not process" in event["message"]
        redis.xack.assert_awaited_once()

    asyncio.run(_test())


@pytest.mark.parametrize("needs_audio", ["true", None])
def test_llm_worker_streams_the_reply_and_queues_speech_only_for_voice(needs_audio):
    async def _test():
        worker = LLMWorker()

        async def answer(prompt, user_id, send):
            await send("It has ")
            await send("shipped.")
            return Answer("It has shipped.", fallback=True, breaker="CLOSED")

        worker.generate_response = answer
        redis = AsyncMock()
        payload = {"session_id": "s1", "prompt": "where is ORD-88412", "trace_id": "v1", "retry_count": "0"}
        if needs_audio:
            payload["needs_audio"] = needs_audio

        await worker.process_message("3-0", payload, redis)

        events = [json.loads(call.args[1]) for call in redis.publish.await_args_list]
        assert [e["token"] for e in events] == ["It has ", "shipped.", ""]
        final = events[-1]
        assert final["is_final"] and final["fallback"] is True and final["circuit_breaker"] == "CLOSED"
        assert final["ttft_ms"] is not None and final["total_latency_ms"] >= final["ttft_ms"]
        speech = [c.args for c in redis.xadd.await_args_list if c.args[0] == settings.STREAM_TTS_SYNTHESIS]
        assert speech == ([(settings.STREAM_TTS_SYNTHESIS, {"session_id": "s1", "text": "It has shipped.",
                                                            "trace_id": "v1"})] if needs_audio else [])
        redis.xack.assert_awaited_once_with(settings.STREAM_LLM_INFERENCE, "llm_group", "3-0")

    asyncio.run(_test())


def test_llm_worker_treats_a_guest_or_malformed_user_id_as_anonymous(monkeypatch):
    import workers.llm_worker as llm_worker_module
    seen = []

    async def fake_answer(raw, clean, user_id, send):
        seen.append(user_id)
        return Answer("ok")

    monkeypatch.setattr(llm_worker_module, "answer_query", fake_answer)

    async def _test():
        worker = LLMWorker()
        for user_id in ("guest", "not-a-uuid", None):
            await worker.generate_response("hi", user_id, AsyncMock())

    asyncio.run(_test())
    assert seen == [None, None, None]


# ----------------------------- Supervisor -----------------------------

def test_supervisor_skips_recent_and_already_claimed_entries():
    async def _test():
        supervisor = WorkerSupervisor()
        supervisor.streams_to_monitor = [(settings.STREAM_STT_AUDIO, "stt_group")]
        redis = AsyncMock()
        redis.xpending = AsyncMock(return_value={"pending": 2})
        redis.xpending_range = AsyncMock(return_value=[
            {"message_id": "1-0", "time_since_delivered": 1000, "times_delivered": 1},   # still fresh
            {"message_id": "2-0", "time_since_delivered": 20000, "times_delivered": 1},  # stale...
        ])
        redis.xclaim = AsyncMock(return_value=[])  # ...but another supervisor claimed it first

        await supervisor.scan_and_reclaim_stranded_tasks(redis)

        redis.xclaim.assert_awaited_once()
        redis.xadd.assert_not_awaited()
        redis.xack.assert_not_awaited()

    asyncio.run(_test())


def test_supervisor_ignores_streams_that_do_not_exist_yet():
    async def _test():
        supervisor = WorkerSupervisor()
        redis = AsyncMock()
        redis.xpending = AsyncMock(side_effect=ResponseError("NOGROUP No such key or consumer group"))

        await supervisor.scan_and_reclaim_stranded_tasks(redis)  # must not raise

        assert redis.xpending.await_count == len(supervisor.streams_to_monitor)

    asyncio.run(_test())


# ----------------------------- Queue producer -----------------------------

def test_publish_stt_task_sends_the_recording_with_its_metadata():
    async def _test():
        manager = QueueManager()
        manager.redis = AsyncMock()

        await manager.publish_stt_task("s1", "AAAA", "v1")

        stream, payload = manager.redis.xadd.await_args.args
        assert stream == settings.STREAM_STT_AUDIO
        assert {k: payload[k] for k in ("session_id", "user_id", "audio_data", "trace_id", "retry_count")} == {
            "session_id": "s1", "user_id": "guest", "audio_data": "AAAA", "trace_id": "v1", "retry_count": "0"}
        assert manager.redis.xadd.await_args.kwargs == {"maxlen": settings.STREAM_MAXLEN, "approximate": True}

    asyncio.run(_test())


def test_consumer_groups_are_created_once_and_real_errors_surface():
    async def _test():
        manager = QueueManager()
        manager.redis = AsyncMock()
        manager.redis.xgroup_create = AsyncMock(side_effect=[None, ResponseError("BUSYGROUP Consumer Group name already exists"), None])

        await manager.init_consumer_groups()

        created = [(c.args[0], c.args[1]) for c in manager.redis.xgroup_create.await_args_list]
        assert created == [(settings.STREAM_STT_AUDIO, "stt_group"), (settings.STREAM_LLM_INFERENCE, "llm_group"),
                           (settings.STREAM_TTS_SYNTHESIS, "tts_group")]

        manager.redis.xgroup_create = AsyncMock(side_effect=ResponseError("WRONGTYPE Operation against a key"))
        with pytest.raises(ResponseError):
            await manager.init_consumer_groups()

    asyncio.run(_test())
