"""
Voxa FastAPI gateway: auth and admin REST endpoints, and the WebSocket the console uses.

``/v1/ws/stream`` accepts two kinds of frames:

* ``text_query``: answered in-process (``app.llm.answer_query``: the intent router picks
  the facts, Gemini writes the reply), streamed back and saved for signed-in users.
* ``audio_chunk``: base64 audio pushed to the STT stream. Worker events (tokens, TTS audio,
  errors) arrive on the Redis Pub/Sub channel ``token:stream:{session_id}`` and are
  forwarded to the socket.
"""
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    create_access_token,
    create_guest_user,
    get_current_admin,
    get_password_hash_async,
    user_id_from_token,
    verify_password_async,
)
from app.config import settings
from app.database import AsyncSessionLocal, engine, get_db, init_db
from app.guardrails import sanitize_user_input
from app.llm import answer_query
from app.models import Conversation, Message, SupportTicket, User
from app.queue_manager import queue_manager, session_channel
from app.rag_engine import rag_engine
from app.rate_limiter import client_identity, rate_limit_dependency, rate_limiter
from app.schemas import (
    AdminMetricsResponse,
    TicketResponse,
    TokenResponse,
    UserCreate,
    UserLogin,
)
from app.seed_data import seed_database
from app.telemetry import (
    WEBSOCKET_ACTIVE_CONNECTIONS,
    TelemetryMiddleware,
    gauge_value,
    get_prometheus_metrics,
    install_log_redaction,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.gateway")
install_log_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Voxa gateway")
    await init_db()
    try:
        await seed_database()
    except Exception as e:
        # Expected when several replicas race to seed the same rows; one of them wins.
        logger.warning(f"Database auto-seeding notice: {e}")
    try:
        await queue_manager.init_consumer_groups()
    except Exception as e:
        logger.warning(f"Redis consumer groups initialization notice: {e}")
    try:
        # Load the embedding model now so the first user query does not pay for it.
        await rag_engine.generate_embedding_async("warm-up")
    except Exception as e:
        logger.warning(f"Embedding model warm-up failed: {e}")
    yield
    logger.info("Shutting down Voxa gateway")
    await engine.dispose()


app = FastAPI(
    title="Voxa gateway",
    version="2.0.0",
    description="Streaming voice and text customer-support API.",
    lifespan=lifespan
)

# CORS for the Next.js frontend. Origins come from configuration so a deployment on a
# public host can allow its own frontend origin without code changes.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=settings.CORS_ALLOW_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-ID", "Retry-After"]
)

# Enable Distributed Tracing & Prometheus Telemetry
app.add_middleware(TelemetryMiddleware)


# ==================== Health & Metrics ====================

@app.get("/health", tags=["System"])
async def health_check():
    """Liveness probe: process is up (does not check Redis/PostgreSQL)."""
    return {
        "status": "healthy",
        "service": "voxa-gateway",
        "version": "2.0.0",
        "timestamp": time.time()
    }


@app.get("/metrics", tags=["Telemetry"])
async def prometheus_metrics():
    """Prometheus scraper endpoint."""
    body, content_type = get_prometheus_metrics()
    return Response(content=body, media_type=content_type)

# ==================== Authentication Endpoints ====================


# Credential endpoints are rate-limited per client IP so passwords (including the seeded
# admin's) cannot be brute-forced.
@app.post("/v1/auth/signup", response_model=TokenResponse, tags=["Authentication"],
          dependencies=[Depends(rate_limit_dependency)])
async def signup(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == user_in.email)
    existing_user = (await db.execute(stmt)).scalar_one_or_none()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")

    new_user = User(
        email=user_in.email,
        hashed_password=await get_password_hash_async(user_in.password),
        role="user"
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    token = create_access_token(data={"sub": str(new_user.id), "role": new_user.role})
    return {"access_token": token, "token_type": "bearer", "user": new_user}


@app.post("/v1/auth/login", response_model=TokenResponse, tags=["Authentication"],
          dependencies=[Depends(rate_limit_dependency)])
async def login(user_in: UserLogin, db: AsyncSession = Depends(get_db)):
    stmt = select(User).where(User.email == user_in.email)
    user = (await db.execute(stmt)).scalar_one_or_none()
    if not user or not await verify_password_async(user_in.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_access_token(data={"sub": str(user.id), "role": user.role})
    return {"access_token": token, "token_type": "bearer", "user": user}


@app.post("/v1/auth/guest-demo", response_model=TokenResponse, tags=["Authentication"],
          dependencies=[Depends(rate_limit_dependency)])
async def guest_demo_login(db: AsyncSession = Depends(get_db)):
    """Creates an ephemeral guest account (rate-limited per IP) and returns its token."""
    user, token = await create_guest_user(db)
    return {"access_token": token, "token_type": "bearer", "user": user}

# ==================== WebSocket ====================


@app.websocket("/v1/ws/stream")
async def websocket_stream(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    Full-duplex WebSocket endpoint (JSON text frames only).

    Client -> server: ``{"type": "text_query", "text", "trace_id"?}`` or
                      ``{"type": "audio_chunk", "audio_data": <base64>, "trace_id"?}``.
    Server -> client: ``token_chunk`` (the streamed reply; the last has ``is_final``),
                      ``ack``, ``audio_response`` (voice only) and ``error`` frames.

    ``?token=<JWT>`` is optional: anonymous sessions work, but only authenticated
    sessions are persisted (conversations belong to a user) and attributed on tickets.
    """
    await websocket.accept()
    WEBSOCKET_ACTIVE_CONNECTIONS.inc()
    session_id = str(uuid.uuid4())
    logger.info(f"Connected WebSocket client [Session: {session_id}]")

    authenticated_user_id = user_id_from_token(token)
    rate_identity = client_identity(websocket.client.host if websocket.client else None, websocket.headers, token)

    # Dedicated Pub/Sub subscription for worker events (voice pipeline tokens, TTS audio)
    pubsub = queue_manager.redis.pubsub()
    channel = session_channel(session_id)

    async def forward_worker_events():
        try:
            async for message in pubsub.listen():
                if message and message.get("type") == "message" and message.get("data"):
                    try:
                        await websocket.send_json(json.loads(message["data"]))
                    except Exception:
                        pass  # malformed worker payload or socket already closing
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Worker Pub/Sub forwarder closed for {session_id}: {e}")

    forwarder_task: Optional[asyncio.Task] = None
    active_conversation_id = None
    conversation_lock = asyncio.Lock()

    async def send_error(message: str, trace_id: Optional[str] = None):
        await websocket.send_json({"type": "error", "message": message, "session_id": session_id, "trace_id": trace_id})

    async def save_turn(question: str, reply: str, ttft_ms: Optional[int], total_latency_ms: Optional[int]):
        """Saves one exchange. Signed-in sessions only: conversations.user_id is NOT NULL."""
        nonlocal active_conversation_id
        if authenticated_user_id is None:
            return
        try:
            async with conversation_lock, AsyncSessionLocal() as db:
                if not active_conversation_id:
                    conversation = Conversation(user_id=authenticated_user_id, title=question[:40])
                    db.add(conversation)
                    await db.commit()
                    await db.refresh(conversation)
                    active_conversation_id = conversation.id
                db.add_all([
                    Message(conversation_id=active_conversation_id, sender="user", content=question),
                    Message(conversation_id=active_conversation_id, sender="assistant", content=reply,
                            ttft_ms=ttft_ms, total_latency_ms=total_latency_ms),
                ])
                await db.commit()
        except Exception as e:
            logger.warning(f"Could not save the conversation turn: {e}")

    try:
        try:
            await pubsub.subscribe(channel)
            forwarder_task = asyncio.create_task(forward_worker_events())
        except Exception as e:
            # Text queries still work without Pub/Sub; only the voice pipeline needs it.
            logger.warning(f"Could not subscribe to worker events for {session_id}: {e}")

        while True:
            frame = await websocket.receive()
            if frame["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(frame.get("code", 1000))
            raw_data = frame.get("text")
            if raw_data is None:
                await send_error("Binary frames are not supported. Send JSON text frames.")
                continue
            try:
                data = json.loads(raw_data)
                if not isinstance(data, dict):
                    raise ValueError("payload must be a JSON object")
            except ValueError:
                await send_error("Malformed JSON payload. Please send a JSON object.")
                continue

            msg_type = data.get("type")
            trace_id = str(data.get("trace_id") or uuid.uuid4())
            start_time = time.time()

            if msg_type == "audio_chunk":
                audio_b64 = data.get("audio_data") or ""
                if not isinstance(audio_b64, str) or not audio_b64:
                    await send_error("audio_chunk requires base64 'audio_data'.", trace_id)
                    continue
                if len(audio_b64) > settings.WS_MAX_AUDIO_CHUNK_B64_CHARS:
                    await send_error("Audio chunk too large.", trace_id)
                    continue
                if not await rate_limiter.check_rate_limit(rate_identity):
                    await send_error("Rate limit exceeded. Please slow down.", trace_id)
                    continue
                try:
                    await queue_manager.publish_stt_task(
                        session_id, audio_b64, trace_id,
                        user_id=str(authenticated_user_id) if authenticated_user_id else None,
                    )
                except Exception as e:
                    logger.error(f"[{trace_id}] Could not enqueue audio: {e}")
                    await send_error("Voice pipeline is unavailable. Please type your question.", trace_id)
                    continue
                await websocket.send_json({
                    "type": "ack",
                    "status": "queued_for_transcription",
                    "session_id": session_id,
                    "trace_id": trace_id
                })

            elif msg_type == "text_query":
                query = data.get("text")
                if not isinstance(query, str) or not query.strip():
                    await send_error("text_query requires non-empty 'text'.", trace_id)
                    continue
                if len(query) > settings.WS_MAX_TEXT_QUERY_CHARS:
                    await send_error(f"Message too long (max {settings.WS_MAX_TEXT_QUERY_CHARS} characters).", trace_id)
                    continue
                if not await rate_limiter.check_rate_limit(rate_identity):
                    await send_error("Rate limit exceeded. Please wait a minute before sending more messages.", trace_id)
                    continue

                clean_query = sanitize_user_input(query)
                first_token_ms: list[int] = []

                async def send_token(token: str, trace_id: str = trace_id, start_time: float = start_time):
                    if not first_token_ms:
                        first_token_ms.append(int((time.time() - start_time) * 1000))
                    await websocket.send_json({
                        "type": "token_chunk",
                        "token": token,
                        "is_final": False,
                        "ttft_ms": first_token_ms[0],
                        "trace_id": trace_id
                    })

                try:
                    answer = await answer_query(query, clean_query, authenticated_user_id, send_token)
                except WebSocketDisconnect:
                    raise
                except Exception as e:
                    logger.error(f"[{trace_id}] Could not answer the question: {e}")
                    await send_error("Sorry, something went wrong while answering. Please try again.", trace_id)
                    continue

                ttft_ms = first_token_ms[0] if first_token_ms else None
                total_latency = int((time.time() - start_time) * 1000)
                await websocket.send_json({
                    "type": "token_chunk",
                    "token": "",
                    "is_final": True,
                    "ttft_ms": ttft_ms,
                    "total_latency_ms": total_latency,
                    "cache_hit": answer.cache_hit,
                    "fallback": answer.fallback,
                    "circuit_breaker": answer.breaker,
                    "trace_id": trace_id
                })
                await save_turn(clean_query, answer.text, ttft_ms, total_latency)

            else:
                await send_error("Unsupported message type. Use 'text_query' or 'audio_chunk'.", trace_id)

    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected [Session: {session_id}]")
    except Exception as e:
        logger.error(f"WebSocket session {session_id} terminated with error: {e}")
    finally:
        if forwarder_task:
            forwarder_task.cancel()
        try:
            await pubsub.unsubscribe()
            await pubsub.aclose()
        except Exception:
            pass
        WEBSOCKET_ACTIVE_CONNECTIONS.dec()

# ==================== Admin ====================


async def _redis_operational_stats() -> tuple[float, int, int]:
    """Returns (cache_hit_ratio, active_workers, queue_depth) from shared Redis state."""
    redis = queue_manager.redis
    hits = float(await redis.get("metric:semantic_cache_hit_total") or 0)
    misses = float(await redis.get("metric:semantic_cache_miss_total") or 0)
    ratio = round(hits / (hits + misses), 2) if (hits + misses) > 0 else 0.0

    active_workers = 0
    async for _ in redis.scan_iter(match="worker:heartbeat:*", count=100):
        active_workers += 1

    # Backlog = entries delivered but not yet ACKed (pending) + entries not yet delivered
    # (lag, Redis >= 7). XLEN would be wrong: acknowledged entries stay in the stream.
    queue_depth = 0
    for stream, group in ((settings.STREAM_STT_AUDIO, "stt_group"), (settings.STREAM_LLM_INFERENCE, "llm_group")):
        try:
            for info in await redis.xinfo_groups(stream):
                if info.get("name") == group:
                    queue_depth += int(info.get("pending") or 0) + int(info.get("lag") or 0)
        except Exception:
            continue  # stream/group not created yet
    return ratio, active_workers, queue_depth


@app.get("/v1/admin/metrics", response_model=AdminMetricsResponse, tags=["Admin"])
async def get_admin_metrics(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    """Operational snapshot. Every value is measured; nothing is defaulted to a target."""
    conv_count = (await db.execute(select(func.count(Conversation.id)))).scalar() or 0
    msg_count = (await db.execute(select(func.count(Message.id)))).scalar() or 0
    avg_ttft = (await db.execute(
        select(func.avg(Message.ttft_ms)).where(Message.ttft_ms.isnot(None))
    )).scalar() or 0.0
    p95_latency = (await db.execute(
        select(func.percentile_cont(0.95).within_group(Message.total_latency_ms.asc()))
        .where(Message.total_latency_ms.isnot(None))
    )).scalar() or 0.0

    try:
        ratio, active_workers, queue_depth = await _redis_operational_stats()
    except Exception as e:
        logger.warning(f"Admin metrics: Redis unavailable ({e})")
        ratio, active_workers, queue_depth = 0.0, 0, 0

    return {
        "active_connections": int(gauge_value(WEBSOCKET_ACTIVE_CONNECTIONS)),
        "queue_depth": queue_depth,
        "cache_hit_ratio": ratio,
        "avg_ttft_ms": round(float(avg_ttft), 1),
        "p95_latency_ms": round(float(p95_latency), 1),
        "active_workers": active_workers,
        "total_conversations": conv_count,
        "total_messages": msg_count
    }


@app.get("/v1/admin/tickets", response_model=list[TicketResponse], tags=["Admin"])
async def list_support_tickets(
    admin: User = Depends(get_current_admin),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).order_by(SupportTicket.created_at.desc()).limit(20)
    tickets = (await db.execute(stmt)).scalars().all()
    return tickets
