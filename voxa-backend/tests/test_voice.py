"""The voice path: transcripts reach the client and voice exchanges are saved."""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.auth import create_access_token
from app.config import settings
from workers.stt_worker import STTWorker


def test_stt_worker_sends_the_transcript_before_queueing_the_answer():
    async def _test():
        worker = STTWorker(load_model=False)
        worker.transcribe_audio_bytes = lambda audio: "where is my order ORD 88412"
        redis = AsyncMock()
        calls = []
        redis.publish.side_effect = lambda channel, data: calls.append(("publish", json.loads(data)))
        redis.xadd.side_effect = lambda stream, fields, **kw: calls.append(("xadd", stream))

        await worker.handle_message(redis, "1-0", {"session_id": "s1", "audio_data": "AAAA", "trace_id": "t1"})

        (kind, event), (next_kind, stream) = calls
        assert kind == "publish" and event["type"] == "transcript"
        assert event["text"] == "where is my order ORD 88412" and event["trace_id"] == "t1"
        assert next_kind == "xadd" and stream == settings.STREAM_LLM_INFERENCE

    asyncio.run(_test())


def test_whisper_is_prompted_with_the_order_id_format():
    """Without a prompt Whisper wrote a spoken 'ORD-88412' as 'or D88412', so no order was found."""
    from unittest.mock import MagicMock

    from workers.stt_worker import WHISPER_PROMPT

    worker = STTWorker(load_model=False)
    worker.model = MagicMock()
    worker.model.transcribe.return_value = ([MagicMock(text=" Where is my order ORD-88412?")], None)

    assert worker.transcribe_audio_bytes(b"RIFF") == "Where is my order ORD-88412?"
    assert worker.model.transcribe.call_args.kwargs["initial_prompt"] == WHISPER_PROMPT
    assert "ORD-" in WHISPER_PROMPT


class ScriptedPubSub:
    """Delivers a fixed list of worker events, then stays subscribed like a real one."""
    def __init__(self, events):
        self.events = events

    async def subscribe(self, *args):
        pass

    async def listen(self):
        for event in self.events:
            yield {"type": "message", "data": json.dumps(event)}
        await asyncio.Event().wait()

    async def unsubscribe(self, *args):
        pass

    async def aclose(self):
        pass


class RecordingSession:
    added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def commit(self):
        pass

    async def refresh(self, obj):
        obj.id = uuid.uuid4()


def test_gateway_saves_voice_exchanges_for_signed_in_users(monkeypatch):
    import app.main as main
    from app.models import Conversation, Message

    events = [
        {"type": "transcript", "text": "where is my order ORD 88412", "trace_id": "v1"},
        {"type": "token_chunk", "token": "It has ", "is_final": False, "ttft_ms": 900, "trace_id": "v1"},
        {"type": "token_chunk", "token": "shipped.", "is_final": False, "ttft_ms": 900, "trace_id": "v1"},
        {"type": "token_chunk", "token": "", "is_final": True, "ttft_ms": 900, "total_latency_ms": 1500,
         "trace_id": "v1"},
    ]
    RecordingSession.added = []
    monkeypatch.setattr(main.queue_manager.redis, "pubsub", lambda: ScriptedPubSub(events))
    monkeypatch.setattr(main, "AsyncSessionLocal", RecordingSession)
    token = create_access_token({"sub": str(uuid.uuid4()), "role": "user"})

    with TestClient(main.app).websocket_connect(f"/v1/ws/stream?token={token}") as ws:
        received = [ws.receive_json() for _ in events]
        ws.send_text("not json")  # one more round trip, so the save has finished
        assert ws.receive_json()["type"] == "error"

    assert [e["type"] for e in received] == ["transcript", "token_chunk", "token_chunk", "token_chunk"]
    conversation, question, reply = RecordingSession.added
    assert isinstance(conversation, Conversation) and conversation.title == "where is my order ORD 88412"
    assert isinstance(question, Message) and question.content == "where is my order ORD 88412"
    assert reply.content == "It has shipped." and reply.ttft_ms == 900 and reply.total_latency_ms == 1500
