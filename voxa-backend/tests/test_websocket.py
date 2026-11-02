"""
The /v1/ws/stream handler: frame validation, the audio path and failures while answering.
Redis and the answer itself are replaced, so these run offline.
"""
import asyncio
import base64
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import create_access_token
from app.llm import Answer
from tests.fakes import DeadPubSub
from tests.test_voice import RecordingSession, ScriptedPubSub


@pytest.fixture
def gateway(monkeypatch):
    """No Pub/Sub, rate limits always pass, and every typed question is answered 'It has shipped.'"""
    async def answer(raw, clean, user_id, send):
        await send("It has shipped.")
        return Answer("It has shipped.")

    monkeypatch.setattr(main.queue_manager.redis, "pubsub", lambda: DeadPubSub())
    monkeypatch.setattr(main.rate_limiter, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "answer_query", answer)
    monkeypatch.setattr(main.queue_manager, "publish_stt_task", AsyncMock(return_value="1-0"))
    return TestClient(main.app)


def ask(ws, text):
    ws.send_json({"type": "text_query", "text": text, "trace_id": "t1"})
    frames = [ws.receive_json()]
    while frames[-1]["type"] == "token_chunk" and not frames[-1]["is_final"]:
        frames.append(ws.receive_json())
    return frames


def test_invalid_frames_get_an_error_and_the_socket_stays_open(gateway):
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        for send, expected in (
            (lambda: ws.send_text("not json"), "Malformed JSON"),
            (lambda: ws.send_text("[1, 2]"), "Malformed JSON"),
            (lambda: ws.send_bytes(b"\x00\x01"), "Binary frames are not supported"),
            (lambda: ws.send_json({"type": "bogus"}), "Unsupported message type"),
            (lambda: ws.send_json({"type": "text_query"}), "requires non-empty 'text'"),
            (lambda: ws.send_json({"type": "text_query", "text": "   "}), "requires non-empty 'text'"),
            (lambda: ws.send_json({"type": "text_query", "text": 42}), "requires non-empty 'text'"),
            (lambda: ws.send_json({"type": "text_query", "text": "x" * 2001}), "Message too long (max 2000"),
            (lambda: ws.send_json({"type": "audio_chunk"}), "requires base64 'audio_data'"),
            (lambda: ws.send_json({"type": "audio_chunk", "audio_data": 123}), "requires base64 'audio_data'"),
        ):
            send()
            frame = ws.receive_json()
            assert frame["type"] == "error" and expected in frame["message"], (expected, frame)

        assert ask(ws, "x" * 2000)[-1]["is_final"]  # exactly at the limit is accepted


def test_a_recording_is_queued_for_transcription_and_acknowledged(gateway):
    user_id = uuid.uuid4()
    token = create_access_token({"sub": str(user_id), "role": "user"})
    audio = base64.b64encode(b"RIFF....WAVE").decode()

    with gateway.websocket_connect(f"/v1/ws/stream?token={token}") as ws:
        ws.send_json({"type": "audio_chunk", "audio_data": audio, "trace_id": "v1"})
        ack = ws.receive_json()

    assert ack["type"] == "ack" and ack["status"] == "queued_for_transcription" and ack["trace_id"] == "v1"
    session_id, sent_audio, trace_id = main.queue_manager.publish_stt_task.await_args.args
    assert sent_audio == audio and trace_id == "v1" and session_id == ack["session_id"]
    assert main.queue_manager.publish_stt_task.await_args.kwargs["user_id"] == str(user_id)


def test_an_oversized_recording_is_rejected_before_it_is_queued(gateway):
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        ws.send_json({"type": "audio_chunk", "audio_data": "A" * (main.settings.WS_MAX_AUDIO_CHUNK_B64_CHARS + 1)})
        assert ws.receive_json()["message"] == "Audio chunk too large."
    main.queue_manager.publish_stt_task.assert_not_awaited()


def test_voice_reports_an_unavailable_pipeline_when_redis_rejects_the_recording(gateway, monkeypatch):
    monkeypatch.setattr(main.queue_manager, "publish_stt_task", AsyncMock(side_effect=ConnectionError("redis down")))
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        ws.send_json({"type": "audio_chunk", "audio_data": "AAAA"})
        assert "Voice pipeline is unavailable" in ws.receive_json()["message"]


def test_rate_limited_questions_and_recordings_get_an_error_frame(gateway, monkeypatch):
    monkeypatch.setattr(main.rate_limiter, "check_rate_limit", AsyncMock(return_value=False))
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        assert "Rate limit exceeded" in ask(ws, "hello")[0]["message"]
        ws.send_json({"type": "audio_chunk", "audio_data": "AAAA"})
        assert "Rate limit exceeded" in ws.receive_json()["message"]
    main.queue_manager.publish_stt_task.assert_not_awaited()


def test_a_failure_while_answering_gets_an_error_frame(gateway, monkeypatch):
    """For example PostgreSQL being down while the router looks up an order."""
    monkeypatch.setattr(main, "answer_query", AsyncMock(side_effect=OSError("database unreachable")))
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        frame = ask(ws, "Where is ORD-88412?")[0]
        assert frame["type"] == "error" and "something went wrong while answering" in frame["message"]
        assert frame["trace_id"] == "t1"


def test_malformed_worker_events_are_skipped(gateway, monkeypatch):
    class MixedPubSub(ScriptedPubSub):
        async def listen(self):
            yield {"type": "subscribe", "data": 1}
            yield {"type": "message", "data": "not json"}
            yield {"type": "message", "data": json.dumps({"type": "transcript", "text": "hi", "trace_id": "v1"})}
            await asyncio.Event().wait()

    monkeypatch.setattr(main.queue_manager.redis, "pubsub", lambda: MixedPubSub([]))
    with gateway.websocket_connect("/v1/ws/stream") as ws:
        assert ws.receive_json() == {"type": "transcript", "text": "hi", "trace_id": "v1"}


def test_a_voice_turn_that_ends_in_an_error_is_not_saved(gateway, monkeypatch):
    events = [
        {"type": "transcript", "text": "where is my order", "trace_id": "v1"},
        {"type": "error", "message": "Sorry, something went wrong while answering.", "trace_id": "v1"},
    ]
    RecordingSession.added = []
    monkeypatch.setattr(main.queue_manager.redis, "pubsub", lambda: ScriptedPubSub(events))
    monkeypatch.setattr(main, "AsyncSessionLocal", RecordingSession)
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "user"})

    with gateway.websocket_connect(f"/v1/ws/stream?token={token}") as ws:
        assert [ws.receive_json()["type"] for _ in events] == ["transcript", "error"]
        ws.send_text("not json")  # one more round trip, so the forwarder has handled both events
        assert ws.receive_json()["type"] == "error"

    assert RecordingSession.added == []
