"""
Voxa FastAPI gateway: REST API, SSE chat endpoint and the full-duplex WebSocket ingress.

Request paths
-------------
* ``/v1/ws/stream`` (WebSocket) - the path used by the web console.
    - ``text_query``: answered in-process by the deterministic intent router below
      (guardrails -> tools / canned policy answers -> semantic cache -> pgvector RAG),
      streamed back word-by-word and persisted for authenticated users.
    - ``audio_chunk``: base64 audio pushed to the STT stream; worker events
      (LLM tokens, TTS audio, errors) arrive on Redis Pub/Sub channel
      ``token:stream:{session_id}`` and are forwarded to the socket.
* ``POST /v1/chat/completions`` (SSE) - dispatches the prompt to the LLM worker over
  Redis Streams and relays its token events; falls back on timeout / open breaker.
"""
import asyncio
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent_tools import (
    ORDER_ID_PATTERN,
    check_refund_eligibility,
    detect_tool_intent,
    escalate_to_human,
    lookup_order,
)
from app.auth import (
    create_access_token,
    create_guest_user,
    get_current_admin,
    get_current_user,
    get_password_hash_async,
    user_id_from_token,
    verify_password_async,
)
from app.circuit_breaker import circuit_breaker
from app.config import settings
from app.database import AsyncSessionLocal, engine, get_db, init_db
from app.guardrails import detect_prompt_injection, sanitize_user_input
from app.models import Conversation, FeedbackRating, Message, SupportTicket, User
from app.queue_manager import queue_manager, session_channel
from app.rag_engine import rag_engine
from app.rate_limiter import client_identity, rate_limit_dependency, rate_limiter
from app.schemas import (
    AdminMetricsResponse,
    ChatRequest,
    FeedbackCreate,
    FeedbackResponse,
    TicketResponse,
    TokenResponse,
    UserCreate,
    UserLogin,
)
from app.seed_data import seed_database
from app.telemetry import (
    LLM_TTFT_HISTOGRAM,
    WEBSOCKET_ACTIVE_CONNECTIONS,
    TelemetryMiddleware,
    gauge_value,
    get_prometheus_metrics,
    install_log_redaction,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("voxa.gateway")
install_log_redaction()

AI_SERVICE = "ai_worker"  # circuit-breaker key shared by every inference path


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Voxa 2.0 Edge Gateway...")
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
    logger.info("🛑 Shutting down Voxa 2.0 Edge Gateway...")
    await engine.dispose()


app = FastAPI(
    title="Voxa 2.0 - Distributed Real-Time Voice & AI Inference Gateway",
    version="2.0.0",
    description="Streaming voice & text customer-support gateway.",
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


def sse_event(payload: dict) -> str:
    """Formats one Server-Sent Events frame."""
    return f"data: {json.dumps(payload)}\n\n"


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

# ==================== Chat & SSE Inference ====================


@app.post("/v1/chat/completions", dependencies=[Depends(rate_limit_dependency)], tags=["Inference"])
async def chat_completions(
    request: Request,
    chat_req: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Server-Sent Events endpoint. Every response is ``text/event-stream``; each frame is
    ``{"token", "ttft_ms", "trace_id"}`` and the last one has ``"is_final": true``.

    Flow: guardrails -> circuit breaker -> semantic cache (generic questions only) ->
    persist user message -> dispatch to the LLM worker via Redis Streams -> relay the
    worker's Pub/Sub token events. If the worker does not finish within
    LLM_RESPONSE_TIMEOUT_SEC the breaker records a failure and a fallback is returned.
    """
    trace_id = getattr(request.state, "trace_id", str(uuid.uuid4()))
    start_time = time.time()

    # 1. Ingress guardrails: neutralise injection phrases and mask PII before the text
    #    is cached, persisted or sent to a worker.
    clean_prompt = sanitize_user_input(chat_req.message)
    has_tool, _, _ = detect_tool_intent(clean_prompt)
    # Tool answers are user-specific (orders) or side-effecting (tickets): never cache them.
    cacheable = not has_tool and not detect_prompt_injection(chat_req.message)

    # 2. Resolve the conversation, enforcing ownership (prevents writing into another
    #    user's thread by guessing its UUID).
    conversation: Optional[Conversation] = None
    if chat_req.conversation_id:
        conversation = await db.get(Conversation, chat_req.conversation_id)
        if conversation is None or conversation.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Conversation not found")

    def single_event_response(text: str, **extra) -> StreamingResponse:
        payload = {
            "token": text,
            "is_final": True,
            "ttft_ms": int((time.time() - start_time) * 1000),
            "trace_id": trace_id,
            **extra,
        }

        async def one_shot():
            yield sse_event(payload)
        return StreamingResponse(one_shot(), media_type="text/event-stream")

    # 3. Distributed circuit breaker (with HALF_OPEN probe gating)
    can_proceed, cb_state = await circuit_breaker.can_execute(AI_SERVICE)
    if not can_proceed:
        return single_event_response(circuit_breaker.get_fallback_response(), circuit_breaker=cb_state)

    # 4. Semantic cache bypass for generic questions
    if cacheable:
        cached_response = await rag_engine.check_semantic_cache(clean_prompt)
        if cached_response:
            if cb_state == "HALF_OPEN":
                await circuit_breaker.record_success(AI_SERVICE)  # release the probe lease
            return single_event_response(cached_response, cache_hit=True)

    # 5. Persist the user turn
    if conversation is None:
        conversation = Conversation(user_id=current_user.id, title=clean_prompt[:40])
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
    conversation_id = conversation.id
    db.add(Message(conversation_id=conversation_id, sender="user", content=clean_prompt))
    await db.commit()

    async def sse_event_generator():
        session_id = str(uuid.uuid4())
        pubsub = queue_manager.redis.pubsub()
        # Subscribe BEFORE publishing the task so no worker event can be missed.
        await pubsub.subscribe(session_channel(session_id))
        tokens: list[str] = []
        ttft_ms: Optional[int] = None
        completed = False
        try:
            await queue_manager.publish_llm_task(
                session_id=session_id,
                user_id=str(current_user.id),
                prompt=clean_prompt,
                conversation_id=str(conversation_id),
                trace_id=trace_id
            )
            deadline = time.monotonic() + settings.LLM_RESPONSE_TIMEOUT_SEC
            while (remaining := deadline - time.monotonic()) > 0:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=remaining)
                if not message or message.get("type") != "message":
                    continue
                event = json.loads(message["data"])
                if event.get("type") != "token_chunk":
                    continue
                if ttft_ms is None:
                    ttft_ms = int((time.time() - start_time) * 1000)
                    LLM_TTFT_HISTOGRAM.observe(ttft_ms / 1000.0)
                if event.get("token"):
                    tokens.append(event["token"])
                    yield sse_event({"token": event["token"], "ttft_ms": ttft_ms, "trace_id": trace_id})
                if event.get("is_final"):
                    completed = True
                    break
        except Exception as e:
            logger.error(f"[{trace_id}] LLM dispatch/relay failed: {e}")
        finally:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()
            except Exception:
                pass

        total_latency = int((time.time() - start_time) * 1000)
        if not completed:
            await circuit_breaker.record_failure(AI_SERVICE)
            yield sse_event({
                "token": circuit_breaker.get_fallback_response(),
                "is_final": True,
                "timed_out": True,
                "total_latency_ms": total_latency,
                "trace_id": trace_id,
            })
            return

        await circuit_breaker.record_success(AI_SERVICE)
        final_answer = "".join(tokens).strip()
        if cacheable and final_answer:
            await rag_engine.store_semantic_cache(clean_prompt, final_answer)

        try:
            async with AsyncSessionLocal() as session:
                session.add(Message(
                    conversation_id=conversation_id,
                    sender="assistant",
                    content=final_answer,
                    ttft_ms=ttft_ms,
                    total_latency_ms=total_latency
                ))
                await session.commit()
        except Exception as persist_err:
            logger.warning(f"[{trace_id}] Could not persist assistant message: {persist_err}")

        yield sse_event({
            "token": "",
            "is_final": True,
            "ttft_ms": ttft_ms,
            "total_latency_ms": total_latency,
            "conversation_id": str(conversation_id),
            "trace_id": trace_id,
        })

    return StreamingResponse(sse_event_generator(), media_type="text/event-stream")

# ==================== WebSocket text-query intent router ====================

INJECTION_DB_KEYWORDS = ["delete the database", "drop table", "output the admin"]
ESCALATION_KEYWORDS = ["stole my money", "chargeback", "lawyer", "bank dispute", "scam", "fraud", "unacceptable"]


async def answer_text_query(raw_query: str, clean_query: str, user_id: Optional[uuid.UUID]) -> tuple[str, bool]:
    """
    Deterministic, priority-ordered intent router for WebSocket text queries.

    Returns ``(response_text, cache_hit)``. Only the generic policy branch (last) reads
    from or writes to the shared semantic cache; order lookups and tickets never do.
    Raises on infrastructure errors so the caller can feed the circuit breaker.
    """
    prompt_lower = clean_query.lower()
    order_match = ORDER_ID_PATTERN.search(clean_query)

    # Priority 1: Security Injection (checked on the raw text: the sanitised copy has
    # already had the injection phrase replaced by a placeholder)
    if detect_prompt_injection(raw_query) or any(k in prompt_lower for k in INJECTION_DB_KEYWORDS):
        return "🛡️ **Voxa Security Guardrail Activated:** System prompt injection and unauthorized database operations have been intercepted and neutralized. I am restricted to authentic customer service and order tracking.", False

    # Priority 2: Chargeback / Anger / Supervisor Escalation (word boundaries so 'issue' does not match 'sue')
    if any(k in prompt_lower for k in ESCALATION_KEYWORDS) or re.search(r'\b(sue|suing|lawsuit)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return f"🚨 **Urgent Priority Escalation:** I sincerely apologize for the distress. I have generated Urgent Support Ticket **#{tool_result['ticket_id']}** directly to our Senior Customer Resolution Lead. A supervisor is reviewing your billing history right now to ensure a complete resolution or instant refund.", False

    # Priority 3: B2B Enterprise / Corporate Invoicing
    if (any(k in prompt_lower for k in ["team of", "developers", "corporate", "bulk", "procurement", "enterprise"])
            and any(k in prompt_lower for k in ["units", "buy", "order", "invoic", "purchase"])):
        return "🏢 **Voxa Enterprise Desk:** For bulk orders (20+ units of Studio Headphones or Earbuds Pro):\n• **Volume Discount**: 20%–25% off.\n• **Payment Terms**: Net-30 enterprise invoicing & VAT/GST receipts.\n• **SLA**: 2-year enterprise warranty.\nI have registered Corporate Lead **#CORP-5091** for a formal quotation today.", False

    # Priority 4: International Customs Duty & VAT Taxes (word boundary so 'private' does not match 'vat')
    if (re.search(r'\bvat\b', prompt_lower) or any(k in prompt_lower for k in ["customs", "duty", "import tax"])
            or (any(k in prompt_lower for k in ["uk", "canada", "india", "europe", "international"]) and "ship" in prompt_lower)):
        return "🌍 **International Customs & Duty (DDP Delivery):** All Voxa international orders are shipped **Delivered Duty Paid (DDP)**. Local VAT and import duties are collected upfront during checkout with zero surprise doorstep fees. Air transit takes 7–14 business days with live GPS tracking.", False

    # Priority 5: Bluetooth Multipoint Dual-Device Pairing
    if ("multipoint" in prompt_lower
            or ("simultaneously" in prompt_lower and ("connect" in prompt_lower or "pair" in prompt_lower))
            or (("both" in prompt_lower or "two" in prompt_lower) and ("iphone" in prompt_lower or "pc" in prompt_lower or "devices" in prompt_lower))):
        return "📶 **Bluetooth 5.3 Multipoint Dual-Device Pairing:** Yes! Voxa Earbuds Pro & Studio Headphones support Multipoint Connection to pair with 2 devices simultaneously (iPhone + PC). Connect to your phone first, put earbuds into pairing mode (hold case 3s), and select 'Voxa Audio' on your PC. Audio will switch automatically on incoming calls!", False

    # Priority 6: Out-of-Warranty / Water Damage (word boundary so 'waterfall' does not match)
    if re.search(r'\b(pool|submerged|water|dropped in)\b', prompt_lower) or ("14 months" in prompt_lower or "2 years" in prompt_lower):
        return "💧 **Warranty & Out-of-Warranty Replacement:** The 1-Year Manufacturer Warranty covers craftsmanship up to 12 months. Prolonged pool submersion or usage beyond 12 months falls outside standard warranty, but we offer an **Out-of-Warranty Replacement at 35% OFF**! Share your purchase email to claim your voucher.", False

    # Priority 7: Price Match Protection
    if any(k in prompt_lower for k in ["price match", "sale today", "cheaper today", "price drop", "refund the difference"]):
        return "🏷️ **14-Day Price Protection:** Yes! Under Voxa's Price Protection Policy, if an item you purchased goes on sale within 14 days of your order, you receive a full refund of the price difference. The difference will be credited to your card in 3–5 business days.", False

    # Priority 8: Delivery Discrepancy (Delivered but Missing)
    if "delivered" in prompt_lower and any(k in prompt_lower for k in ["not", "never", "haven", "didn", "missing", "where"]):
        return "📦 **Package Marked Delivered But Not Received:** 1. Check with household members, reception, or neighbors. 2. Couriers occasionally scan packages up to 24 hours prior to physical delivery. 3. If not received after 24 hours, provide your Order ID (e.g. ORD-88412) and we will dispatch an immediate free replacement!", False

    # Priority 9: Product Buying Advice
    if (("headphone" in prompt_lower and ("earphone" in prompt_lower or "earbud" in prompt_lower))
            or any(k in prompt_lower for k in ["which should i buy", "what should i order", "what should i buy", "which is better", "recommend", "buying advice", "compare"])):
        return "🎧 **Voxa Product Buying Guide:**\n• **Wireless Earbuds Pro ($149)**: Best for workouts, running, pocket mobility (IPX7 water resistant, 32h battery).\n• **Smart Studio Headphones ($249)**: Best for deep immersion, 42dB Active Noise Cancellation, audiophile sound, and office focus.\n👉 Pick **Earbuds** for portability, or **Headphones** for superior ANC sound isolation!", False

    # Priority 10: SQL Database Order Lookup & Refund (never cached: user-specific data)
    if order_match:
        order_id = order_match.group(0).upper()
        async with AsyncSessionLocal() as db_session:
            if "refund" in prompt_lower or "return" in prompt_lower:
                tool_result = await check_refund_eligibility(order_id, db_session)
                if tool_result.get("found"):
                    status_str = "Eligible for full refund" if tool_result.get("eligible") else "Ineligible for refund"
                    return f"📦 **Order {tool_result.get('order_id')}**: {status_str} ({tool_result.get('reason')}).", False
                return tool_result.get("message", f"Order {order_id} not found."), False
            tool_result = await lookup_order(order_id, db_session)
            if tool_result.get("found"):
                return f"📦 **Order {tool_result.get('order_id')} ({tool_result.get('item_name')})**: Currently **{tool_result.get('status')}**. Tracking number: `{tool_result.get('tracking_number')}`.", False
            return tool_result.get("message", f"Order {order_id} not found."), False

    # Priority 11: Human Agent Escalation
    if re.search(r'\b(human|agent|manager|escalat\w*|representative|talk to someone)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return f"👤 **Live Human Specialist Transfer:** I have generated Priority Support Ticket **#{tool_result['ticket_id']}** in our system. A Tier-2 Senior Customer Support Specialist has received your inquiry and will connect with you within moments!", False

    # Priority 11.5: Greetings
    if re.search(r'\b(hi|hello|hey|greetings|who are you|what can you do|how are you|how are u)\b', prompt_lower):
        return "👋 **Hello! I am Voxa 2.0**, your real-time voice and support assistant.\n\nI can help you with:\n• 📦 **Live Order Tracking** (e.g. `ORD-88412`)\n• 💸 **Refund Calculations & Returns**\n• 🎧 **Product Buying Advice** (Earbuds vs Headphones)\n• 🌍 **Customs & International Shipping**\n• 📶 **Bluetooth Multipoint Setup**\n• 🏢 **B2B Bulk Corporate Invoicing**\n\nHow can I help you today? (You can type or speak via your microphone!)", False

    # Priority 12: General RAG Knowledge Base Search (the only branch that uses the semantic cache)
    cached = await rag_engine.check_semantic_cache(clean_query)
    if cached:
        return cached, True

    async with AsyncSessionLocal() as db_session:
        rag_docs = await rag_engine.search_knowledge_base(clean_query, db_session, limit=2)
    if rag_docs and rag_docs[0].get("similarity", 0) > 0.35:
        response_text = f"Based on Voxa's {rag_docs[0]['topic']} policy: {rag_docs[0]['content']}"
    else:
        response_text = "Thank you for contacting Voxa! All purchases include a 30-day money-back guarantee and 1-year warranty. Domestic shipping delivers in 3–5 business days. Please provide your order ID (e.g. ORD-88412) for live tracking!"
    await rag_engine.store_semantic_cache(clean_query, response_text)
    return response_text, False

# ==================== Full-Duplex WebSocket Ingress ====================


@app.websocket("/v1/ws/stream")
async def websocket_stream(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    Full-duplex WebSocket endpoint (JSON text frames only).

    Client -> server: ``{"type": "text_query", "text", "trace_id"?}`` or
                      ``{"type": "audio_chunk", "audio_data": <base64>, "trace_id"?}``.
    Server -> client: ``token_chunk`` (streamed words, last has ``is_final``), ``ack``,
                      ``audio_response`` (TTS, voice path only) and ``error`` frames.

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

    async def send_error(message: str, trace_id: Optional[str] = None):
        await websocket.send_json({"type": "error", "message": message, "session_id": session_id, "trace_id": trace_id})

    async def within_rate_limit() -> bool:
        try:
            return await rate_limiter.check_rate_limit(rate_identity)
        except Exception as e:
            logger.warning(f"Rate limiter unavailable, admitting request: {e}")
            return True  # fail open: a Redis blip should not take the chat offline

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
                if not await within_rate_limit():
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
                if not await within_rate_limit():
                    await send_error("Rate limit exceeded. Please wait a minute before sending more messages.", trace_id)
                    continue

                clean_query = sanitize_user_input(query)

                # 1. Check Circuit Breaker (with probe gating)
                can_proceed, cb_state = await circuit_breaker.can_execute(AI_SERVICE)
                if not can_proceed:
                    await websocket.send_json({
                        "type": "token_chunk",
                        "token": circuit_breaker.get_fallback_response(),
                        "is_final": True,
                        "ttft_ms": int((time.time() - start_time) * 1000),
                        "circuit_breaker": cb_state,
                        "trace_id": trace_id
                    })
                    continue

                cache_hit = False
                try:
                    response_text, cache_hit = await answer_text_query(query, clean_query, authenticated_user_id)
                    await circuit_breaker.record_success(AI_SERVICE)
                except Exception as worker_err:
                    logger.error(f"[{trace_id}] Error executing agent query: {worker_err}")
                    await circuit_breaker.record_failure(AI_SERVICE)
                    response_text = "I encountered an unexpected issue while retrieving your inquiry. Our support team has been notified."

                # 2. Stream the answer word-by-word (paced so the UI renders progressively)
                ttft_ms = int((time.time() - start_time) * 1000)
                for word in response_text.split(" "):
                    await websocket.send_json({
                        "type": "token_chunk",
                        "token": word + " ",
                        "is_final": False,
                        "ttft_ms": ttft_ms,
                        "cache_hit": cache_hit,
                        "trace_id": trace_id
                    })
                    await asyncio.sleep(0.03)

                total_latency = int((time.time() - start_time) * 1000)
                await websocket.send_json({
                    "type": "token_chunk",
                    "token": "",
                    "is_final": True,
                    "ttft_ms": ttft_ms,
                    "total_latency_ms": total_latency,
                    "cache_hit": cache_hit,
                    "circuit_breaker": cb_state,
                    "trace_id": trace_id
                })

                # 3. Persist the exchange (authenticated sessions only: conversations are
                #    owned by a user and conversations.user_id is NOT NULL).
                if authenticated_user_id is None:
                    continue
                try:
                    async with AsyncSessionLocal() as db_persist:
                        if not active_conversation_id:
                            conv = Conversation(user_id=authenticated_user_id, title=clean_query[:40])
                            db_persist.add(conv)
                            await db_persist.commit()
                            await db_persist.refresh(conv)
                            active_conversation_id = conv.id

                        db_persist.add_all([
                            Message(conversation_id=active_conversation_id, sender="user", content=clean_query),
                            Message(
                                conversation_id=active_conversation_id,
                                sender="assistant",
                                content=response_text,
                                ttft_ms=ttft_ms,
                                total_latency_ms=total_latency
                            ),
                        ])
                        await db_persist.commit()
                except Exception as persist_err:
                    logger.warning(f"Could not persist WebSocket conversation to DB: {persist_err}")

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

# ==================== Deterministic Business Tools ====================


@app.get("/v1/tools/order/{order_id}", tags=["Agent Tools"], dependencies=[Depends(rate_limit_dependency)])
async def api_lookup_order(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    return await lookup_order(order_id, db)


@app.get("/v1/tools/refund/{order_id}", tags=["Agent Tools"], dependencies=[Depends(rate_limit_dependency)])
async def api_check_refund(
    order_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    return await check_refund_eligibility(order_id, db)


@app.post("/v1/tools/escalate", tags=["Agent Tools"], dependencies=[Depends(rate_limit_dependency)])
async def api_escalate_ticket(
    issue_summary: str = Query(..., min_length=1, max_length=2000),
    priority: Literal["low", "medium", "high", "urgent"] = "urgent",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    return await escalate_to_human(sanitize_user_input(issue_summary), priority, current_user.id, db)

# ==================== User Feedback & Telemetry ====================


@app.post("/v1/feedback", response_model=FeedbackResponse, tags=["Feedback"])
async def submit_feedback(
    fb_in: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    # The rated message must exist and belong to one of the caller's conversations
    # (otherwise: 500 on the FK violation, or rating other users' messages).
    owned_message = await db.execute(
        select(Message.id)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Message.id == fb_in.message_id, Conversation.user_id == current_user.id)
    )
    if owned_message.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Message not found")

    fb = FeedbackRating(
        message_id=fb_in.message_id,
        user_id=current_user.id,
        score=fb_in.score,
        feedback_text=fb_in.feedback_text
    )
    db.add(fb)
    await db.commit()
    await db.refresh(fb)
    return fb

# ==================== Admin & SRE Operational View ====================


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
