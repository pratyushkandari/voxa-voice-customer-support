<div align="center">

# Voxa: Real-Time Voice AI Customer Support Platform
### Streaming, Fault-Tolerant, Distributed Voice & RAG Support Gateway

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg?logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![Next.js](https://img.shields.io/badge/Next.js-14.2-black.svg?logo=next.js&logoColor=white)](https://nextjs.org)
[![Redis](https://img.shields.io/badge/Redis-Streams%20%26%20Cache-red.svg?logo=redis&logoColor=white)](https://redis.io)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-pgvector-336791.svg?logo=postgresql&logoColor=white)](https://github.com/pgvector/pgvector)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg?logo=docker&logoColor=white)](https://docker.com)
[![Prometheus](https://img.shields.io/badge/Prometheus-Telemetry-E6522C.svg?logo=prometheus&logoColor=white)](https://prometheus.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## 📌 Executive Architecture Summary

**Voxa** is a distributed, event-driven customer-support platform. Instead of HTTP-polling a monolithic bot, clients hold a full-duplex WebSocket to a FastAPI gateway; answers stream back word-by-word, while slow work (speech-to-text, answering for REST and voice clients, speech synthesis) is decoupled into independently scalable workers connected by **Redis Streams consumer groups**. Every channel answers with **one deterministic, priority-ordered intent router** (guardrails, order and refund tools, escalation tickets, canned policies, then knowledge-base retrieval); there is no generative LLM. Knowledge retrieval uses **PostgreSQL + pgvector (HNSW)**, repeated policy questions are served from a **Redis semantic cache**, and a **Redis-backed circuit breaker**, **dead-letter queue** and **crash-recovery supervisor** keep failures contained.

### Design Targets & Reliability Parameters
| Area | What the code does | How to verify |
| :--- | :--- | :--- |
| **Time-to-First-Token** | Target `<180ms`. Every answer carries a measured `ttft_ms`; text queries are routed in-process by the gateway. | Telemetry HUD in the console, `/v1/admin/metrics`, `llm_time_to_first_token_seconds` |
| **Concurrency** | Async WebSocket gateway; `tests/load_test_websockets.js` ramps to 500 concurrent sockets. | Run the k6 script (no benchmark results are committed) |
| **Semantic cache** | Generic policy answers are cached with their embedding; a new query hits when cosine similarity `≥ 0.92`. Order/ticket answers are never cached. | Cache-hit badge per message, live hit ratio in the admin console |
| **Speech-to-text** | Faster-Whisper on CTranslate2 with `compute_type="int8"` on CPU. | `voxa-backend/workers/stt_worker.py` |
| **Resilience** | Circuit breaker trips at `≥50%` failures (min. 4 calls in a 10s window), probes again after 15s; tasks are dead-lettered after 3 attempts. | `tests/test_regressions.py`, `tests/test_e2e_stack.py` |

---

## 🏛️ System Architecture

```mermaid
flowchart TB
    subgraph Client ["Client Layer"]
        Browser["Next.js 14 Console<br/>(WebSocket + browser speech recognition)"]
    end

    subgraph Gateway ["FastAPI Gateway (:8088 → container :8000)"]
        WS_Handler["WebSocket Ingress<br/>/v1/ws/stream"]
        REST["REST + SSE<br/>/v1/chat/completions"]
        Guardrails["PII Masking & Injection Guardrails"]
        RateLimiter["Sliding-Window Rate Limiter<br/>(Redis Lua)"]
        Router["Deterministic Intent Router<br/>(tools · canned policies · RAG)"]
        CircuitBreaker{"3-State Circuit Breaker<br/>(Redis)"}
    end

    subgraph Streaming ["Event Bus & Caching (Redis 7)"]
        StreamSTT["stream:audio:transcribe"]
        StreamLLM["stream:llm:inference"]
        StreamTTS["stream:tts:synthesize"]
        PubSub["Pub/Sub token:stream:{session_id}"]
        SemanticCache[("Semantic Cache<br/>(cosine ≥ 0.92)")]
        DLQ["stream:dead:letter:queue"]
    end

    subgraph Workers ["Decoupled Workers"]
        STTWorker["STT Worker<br/>Faster-Whisper (INT8)"]
        LLMWorker["LLM Worker<br/>same intent router"]
        TTSWorker["TTS Worker<br/>edge-tts"]
        Supervisor["Worker Supervisor<br/>(PEL reclaim → DLQ)"]
    end

    subgraph Storage ["Persistent Data Layer"]
        Postgres[("PostgreSQL 16 + pgvector<br/>users · orders · conversations · tickets · KB")]
    end

    Browser <==>|"JSON frames"| WS_Handler
    WS_Handler --> RateLimiter --> Guardrails --> CircuitBreaker --> Router
    Router <-->|"generic questions"| SemanticCache
    Router <-->|"orders · tickets · HNSW search"| Postgres
    WS_Handler -->|"audio_chunk"| StreamSTT --> STTWorker -->|"masked transcript"| StreamLLM
    REST --> StreamLLM
    StreamLLM --> LLMWorker <-->|"tools · cache · RAG"| Postgres
    LLMWorker -->|"voice requests"| StreamTTS --> TTSWorker
    LLMWorker -->|"tokens"| PubSub
    TTSWorker -->|"audio_response"| PubSub
    PubSub --> WS_Handler
    PubSub --> REST
    Supervisor -.->|"XPENDING / XCLAIM"| StreamLLM
    Supervisor -.->|"poison payloads"| DLQ
```

**How data flows:** the browser opens a WebSocket and sends `text_query` frames. The gateway rate-limits, masks PII, checks the circuit breaker and runs the intent router in-process (`app/agent_tools.py`), which reads orders and tickets from PostgreSQL and answers generic questions from the semantic cache or a pgvector search. API clients that use REST SSE or send audio go through Redis Streams instead: the STT worker transcribes audio, and the LLM worker runs the **same router** and publishes tokens on a per-session Pub/Sub channel that the gateway relays back.

---

## 🛠️ Core Engineering Highlights

### 1. Streaming Ingress & Worker Pipeline
* **Full-duplex WebSocket (`/v1/ws/stream`):** JSON `text_query` / `audio_chunk` frames in; `token_chunk`, `ack`, `audio_response` and `error` frames out. Text queries are answered by the gateway's priority-ordered intent router and streamed word-by-word, each frame carrying `ttft_ms` and a `trace_id`.
* **Voice path:** `audio_chunk` (base64 audio) → `stream:audio:transcribe` → STT worker (Faster-Whisper INT8, inference off the event loop, transcript PII-masked) → `stream:llm:inference` → LLM worker → tokens on Redis Pub/Sub, plus `stream:tts:synthesize` → TTS worker → `audio_response`. The bundled web console uses the browser's speech recognition for microphone input, so this server-side path is exercised by API clients.
* **REST SSE (`POST /v1/chat/completions`):** dispatches the prompt to the LLM worker over Redis Streams and relays its Pub/Sub tokens as Server-Sent Events (the final event carries `cache_hit` and `conversation_id`); times out to a fallback after `LLM_RESPONSE_TIMEOUT_SEC` and feeds the circuit breaker.

### 2. RAG & Redis Semantic Vector Cache
* **Tier 1 (Redis semantic cache):** generic questions are embedded with `sentence-transformers/all-MiniLM-L6-v2` (384-d, normalised) and compared against cached embeddings by dot product; `≥ 0.92` returns the cached answer. Lookups use bounded `SCAN` + one `MGET` (never `KEYS`).
* **Tier 2 (pgvector):** cache misses run an HNSW cosine search (`<=>`, `vector_cosine_ops`) over the knowledge base.
* **Isolation:** order lookups, refund checks and escalation tickets are user-specific or side-effecting, so they never read from or write to the shared cache.

### 3. Distributed Resilience & Fault Tolerance
* **3-state circuit breaker (Redis, shared by all replicas):**
  * `CLOSED`: success/failure outcomes are counted in a 10-second window; with at least 4 calls and a failure ratio `≥ 50%` it trips.
  * `OPEN`: requests get an immediate fallback message for 15 seconds.
  * `HALF_OPEN`: exactly one canary request is admitted via an atomic `SET NX EX` lease; success closes the breaker, failure re-opens it.
* **Redis outages:** the breaker and the rate limiter fail open (state `UNKNOWN`, warning logged), so answers that do not need Redis keep working; limits are not enforced until Redis returns.
* **Retries & dead-letter queue:** workers re-enqueue a failed task with `retry_count + 1`; after 3 attempts it goes to `stream:dead:letter:queue` with the error and origin stream, and the client receives an `error` frame.
* **Crash recovery:** `worker_supervisor` asks each consumer group's Pending Entries List for entries idle for more than 15s (crashed consumer), `XCLAIM`s and re-enqueues them, carrying the attempt count so a crash-looping payload still reaches the DLQ. Healthy workers refresh their claim (`XCLAIM … JUSTID`) every 5s on in-flight work, so long transcriptions are not duplicated.
* **Rate limiting:** an atomic Lua sliding-window log in Redis, keyed by authenticated user (or client IP), default 30 requests/minute. Covers login, sign-up, guest sessions, chat and tool endpoints; REST returns `429` + `Retry-After: 60`, the WebSocket an `error` frame.
* **Bounded streams:** every `XADD` uses `MAXLEN ~ 10000` so acknowledged entries do not accumulate forever.

### 4. Security
* **Guardrails:** prompt-injection phrases are neutralised and card numbers / phone numbers / SSNs are masked before text (typed or transcribed) is routed, embedded, cached, persisted or queued.
* **Auth:** bcrypt password hashing (off the event loop) and signed HS256 JWTs. There is no hard-coded signing key: set `JWT_SECRET_KEY`, otherwise each process generates a random one. The WebSocket token (sent as `?token=`, because browsers cannot set WebSocket headers) is redacted from server logs.
* **Authorization:** admin endpoints require the `admin` role; conversations and feedback are ownership-checked; an order that belongs to a user is visible only to that user (seeded demo orders have no owner and are public); agent-tool endpoints require authentication.
* **Input bounds:** WebSocket text (2,000 chars) and audio (4 MB base64) payloads are size-limited; malformed or unknown frames get an `error` reply instead of closing the session.
* **Containers:** Python images run as uid `1001`, the frontend as the `node` user; `.dockerignore` keeps `.env` files out of images. Compose publishes only the frontend (3000) and gateway (8088); PostgreSQL, Redis, Prometheus and Grafana listen on `127.0.0.1`.

### 5. Telemetry & Observability
* **Prometheus (`/metrics`):** `http_request_duration_seconds` (labelled by route template, not raw path), `llm_time_to_first_token_seconds`, `websocket_active_connections`, `semantic_cache_hit_total`, `semantic_cache_miss_total`.
* **Admin console (`/admin`, admin login):** average TTFT, p95 end-to-end latency (`percentile_cont`), cache hit ratio, live worker heartbeats, queue backlog (pending + lag from `XINFO GROUPS`), open WebSockets, and the latest escalation tickets, refreshed every 10s.
* **Telemetry HUD:** per-answer TTFT, stream throughput, end-to-end latency, circuit-breaker state and correlation `trace_id`.

---

## 🔌 API Reference

Interactive OpenAPI docs are served at `http://localhost:8088/docs`.

| Method & path | Auth | Purpose |
| :--- | :--- | :--- |
| `GET /health` | – | Liveness probe (does not check Redis/PostgreSQL) |
| `GET /metrics` | – | Prometheus exposition |
| `POST /v1/auth/signup` | – (rate-limited) | Create a user `{email, password}` → `{access_token, token_type, user}` |
| `POST /v1/auth/login` | – (rate-limited) | Sign in `{email, password}` → `{access_token, token_type, user}` |
| `POST /v1/auth/guest-demo` | – (rate-limited) | Create an ephemeral guest account → `{access_token, token_type, user}` |
| `POST /v1/chat/completions` | Bearer | SSE answer for `{message, conversation_id?}`; frames `{token, ttft_ms, trace_id}`, last has `is_final` |
| `WS /v1/ws/stream?token=<JWT>` | optional | Streaming chat (frames described above); only authenticated sessions are persisted |
| `GET /v1/tools/order/{order_id}` | Bearer | Order status (owner-scoped) |
| `GET /v1/tools/refund/{order_id}` | Bearer | 30-day refund eligibility (owner-scoped) |
| `POST /v1/tools/escalate?issue_summary=…&priority=…` | Bearer | Create a support ticket (`low` / `medium` / `high` / `urgent`) |
| `POST /v1/feedback` | Bearer | Rate one of your own assistant messages `{message_id, score 1–5, feedback_text?}` |
| `GET /v1/admin/metrics` | Bearer, admin | Operational snapshot used by `/admin` |
| `GET /v1/admin/tickets` | Bearer, admin | Latest 20 escalation tickets |

Rate-limited REST endpoints also honour an optional `X-Idempotency-Key` header (duplicates within 60s get `409`).

---

## 📂 Repository Structure

```
voxa-voice-customer-support/
├── voxa-backend/                       # FastAPI gateway + Redis Streams workers
│   ├── app/
│   │   ├── main.py                     # REST/SSE routes, WebSocket ingress, admin metrics
│   │   ├── agent_tools.py              # Intent router shared by gateway + LLM worker; order, refund, escalation tools
│   │   ├── config.py                   # Environment-driven settings (pydantic-settings)
│   │   ├── auth.py                     # bcrypt + JWT auth, guest accounts, role checks
│   │   ├── guardrails.py               # PII masking & prompt-injection neutralisation
│   │   ├── rate_limiter.py             # Redis Lua sliding-window limiter + idempotency keys
│   │   ├── circuit_breaker.py          # Distributed 3-state circuit breaker
│   │   ├── rag_engine.py               # Semantic cache (Redis) + pgvector HNSW retrieval
│   │   ├── queue_manager.py            # Redis Streams producer, consumer-group bootstrap, claim keep-alive
│   │   ├── telemetry.py                # Prometheus metrics, correlation-ID middleware, log redaction
│   │   ├── database.py / models.py     # Async SQLAlchemy engine, ORM models, schema bootstrap
│   │   ├── schemas.py                  # Pydantic API contracts
│   │   └── seed_data.py                # Idempotent demo users, orders and knowledge base
│   ├── workers/
│   │   ├── stt_worker.py               # Faster-Whisper INT8 transcription consumer
│   │   ├── llm_worker.py               # Answers REST/voice prompts with the shared router
│   │   ├── tts_worker.py               # edge-tts synthesis consumer
│   │   └── worker_supervisor.py        # PEL crash recovery & DLQ routing
│   ├── tests/                          # pytest suite (unit, regression, opt-in end-to-end) + k6 script
│   ├── k8s/                            # Kubernetes manifests (illustrative; not used by the Azure deployment)
│   ├── telemetry/prometheus.yml        # Prometheus scrape config
│   ├── Dockerfile.gateway / Dockerfile.worker
│   └── requirements.txt / requirements-gateway.txt
│
├── voxa-frontend/                      # Next.js 14 App Router frontend
│   ├── app/page.tsx                    # Landing page (guest session + gateway health)
│   ├── app/chat/page.tsx               # Support console: streaming chat, voice input, telemetry HUD
│   ├── app/admin/page.tsx              # Operations console (admin sign-in, live metrics, tickets)
│   ├── components/                     # VoiceOrb, AudioWaveform, TelemetryHUD, RichText
│   └── Dockerfile                      # Multi-stage production build
│
├── docs/ARCHITECTURE.md, docs/RUNBOOK.md
├── PROJECT_HANDBOOK.html               # Self-contained architecture & interview handbook (snapshot)
├── docker-compose.yml                  # Full local / single-VM stack
└── README.md
```

---

## 📖 Interactive Architecture & SDE-1 Interview Handbook

The repository includes a self-contained, offline handbook in [`PROJECT_HANDBOOK.html`](PROJECT_HANDBOOK.html) (open it from the repository root so its source links resolve), organised in three layers:
* **Project at a glance:** purpose, 30-second / 2-minute / 5-minute pitches, technology stack, architecture diagram, setup and current limitations.
* **Implementation handbook:** file-by-file guide with line-referenced source links, end-to-end request workflows, API / WebSocket protocol and data model, AI/ML components, Docker Compose deployment on an Azure Linux VM, configuration, testing, security and ranked improvements.
* **Interview preparation:** a 74-question bank with structured model answers, a scored 15-question mock interview, technology comparisons and system-design practice, plus an evidence ledger and validation report.

The handbook is a snapshot of commit `94b2579`; a notice at its top lists what has changed since.

---

## 🚀 Quickstart & Deployment

### Prerequisites
* **Docker Engine** `v24.0+` & **Docker Compose** `v2.20+`
* *(Optional for running on the host)* Python 3.11+, Node.js 20+, PostgreSQL 16 with pgvector, Redis 7+

### 1. Clone & Configure Environment
```bash
git clone https://github.com/pratyushkandari/voxa-voice-customer-support.git
cd voxa-voice-customer-support
```

Optionally create a root `.env` file; Docker Compose substitutes these values:

| Variable | Default | Notes |
| :--- | :--- | :--- |
| `JWT_SECRET_KEY` | random per gateway start | Set a stable value (`openssl rand -hex 32`), otherwise tokens expire on every restart |
| `SEED_ADMIN_PASSWORD` | `adminpassword123` | Password of `admin@voxa.ai`. Applied only when the admin user is first created |
| `POSTGRES_PASSWORD` | `postgres` | Applied only when the database volume is first created; use URL-safe characters |
| `GRAFANA_ADMIN_PASSWORD` | `admin` | Grafana `admin` user |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8088` | Gateway URL as seen by the browser; baked in at **build** time |
| `NEXT_PUBLIC_WS_URL` | derived from the API URL | Only if the WebSocket lives elsewhere |
| `CORS_ALLOW_ORIGIN_REGEX` | any `localhost` / `127.0.0.1` port | Browser origins allowed to call the REST API |

Example for a cloud VM at `203.0.113.10`:
```env
JWT_SECRET_KEY=<output of: openssl rand -hex 32>
SEED_ADMIN_PASSWORD=<a strong password>
NEXT_PUBLIC_API_URL=http://203.0.113.10:8088
CORS_ALLOW_ORIGIN_REGEX=^https?://203\.0\.113\.10(:[0-9]+)?$
```
Rebuild the frontend (`docker compose build frontend`) after changing `NEXT_PUBLIC_*`. Every backend setting (rate limit, model names, timeouts, stream names) is documented in [`voxa-backend/.env.example`](voxa-backend/.env.example). Browsers only allow microphone access on `https://` or `localhost`, so voice input on a plain-HTTP public IP needs HTTPS in front of the frontend.

### 2. Start All Services via Docker Compose
```bash
docker compose up --build -d
```
The first start downloads the embedding and Whisper models into the containers; the TTS worker needs outbound internet access for edge-tts.

### 3. Verify System Health
| Service | Endpoint | Description |
| :--- | :--- | :--- |
| **Frontend UI** | `http://localhost:3000` | Landing page → support console |
| **Operations Console** | `http://localhost:3000/admin` | Sign in as `admin@voxa.ai` (`SEED_ADMIN_PASSWORD`) |
| **FastAPI Gateway** | `http://localhost:8088/health` | Liveness probe (`/docs` for the OpenAPI UI) |
| **Prometheus Metrics**| `http://localhost:8088/metrics` | Raw Prometheus exposition |
| **Prometheus** | `http://127.0.0.1:9090` | Scrapes the gateway every 5s (host-local only) |
| **Grafana** | `http://127.0.0.1:3001` | Empty Grafana instance (add Prometheus as a data source; no dashboards are provisioned; host-local only) |

On a remote VM, reach the host-local services through an SSH tunnel, e.g. `ssh -L 3001:127.0.0.1:3001 user@vm`.

Seeded demo data: orders `ORD-88412` (shipped), `ORD-10294` (outside the refund window), `ORD-99120` (processing), plus a demo user `demo@voxa.ai` / `demopassword123`.

---

## 🧪 Testing & Verification

### Backend unit & regression tests (hermetic: no Redis, PostgreSQL or model downloads)
```bash
cd voxa-backend
pip install -r requirements.txt
pytest -v
```
`tests/conftest.py` swaps the embedding model for a deterministic test double (and stubs Faster-Whisper / edge-tts when they are not installed); `tests/test_regressions.py` pins every defect fixed in the repository audits.

### End-to-end tests against a running stack
```bash
docker compose up -d                      # from the repository root
cd voxa-backend
VOXA_E2E_BASE_URL=http://localhost:8088 pytest tests/test_e2e_stack.py -v
```
These drive the real gateway, PostgreSQL/pgvector, Redis and LLM worker: WebSocket intents, semantic-cache hits, persistence, admin metrics, SSE through the worker, ownership checks and rate limiting. Set `VOXA_E2E_ADMIN_PASSWORD` if you changed `SEED_ADMIN_PASSWORD`.

### Frontend
```bash
cd voxa-frontend
npm ci
npm run lint && npm run typecheck && npm run build
```
The frontend has no automated tests.

### Load testing the WebSocket ingress (k6)
```bash
k6 run -e WS_URL=ws://localhost:8088/v1/ws/stream voxa-backend/tests/load_test_websockets.js
```
Raise `RATE_LIMIT_REQUESTS_PER_MINUTE` on the gateway under test first: all virtual users share one client IP.

---

## 🏛️ Architecture Decision Records (ADRs)

### ADR-001: Task Transport — Redis Streams vs. Apache Kafka
* **Context:** Real-time voice inference demands low-latency task dispatch to inference workers with minimal broker overhead.
* **Decision:** Selected **Redis Streams** with consumer groups over Apache Kafka.
* **Trade-off Analysis:** Kafka offers durable multi-day log retention and replay, but adds a JVM cluster and operational weight that a single interactive workload does not need. Redis Streams gives consumer-group delivery (`XREADGROUP`), explicit acknowledgment (`XACK`) and a pending-entries list for crash recovery (`XPENDING`/`XCLAIM`), while the same Redis also serves the cache, rate limiter, circuit breaker and Pub/Sub. The cost is weaker durability: streams are memory-bound (hence `MAXLEN`) and depend on Redis persistence settings.

### ADR-002: Vector Search & ACID Relational Storage — PostgreSQL (pgvector) vs. Standalone Vector DBs
* **Context:** The platform requires relational customer data (orders, conversations, support tickets) combined with 384-dimensional semantic knowledge embeddings.
* **Decision:** Selected **PostgreSQL 16 with `pgvector`** over external vector databases (Pinecone, Chroma, Milvus).
* **Trade-off Analysis:** External vector engines introduce dual writes, cross-system consistency problems and extra network hops. PostgreSQL with `pgvector` keeps relational queries and cosine-distance search (`<=>`, HNSW index) in one transactional store, simplifying backups and connection pooling, at the cost of the scale ceilings of a single Postgres primary.

### ADR-003: Model Execution Runtime — CTranslate2 Faster-Whisper INT8 vs. Vanilla PyTorch
* **Context:** Speech-to-text must run on commodity CPU instances without dedicated GPUs.
* **Decision:** Deployed **Faster-Whisper running on CTranslate2 with INT8 quantization**.
* **Trade-off Analysis:** INT8 weights substantially reduce memory and CPU time compared with FP32 PyTorch Whisper, with a small accuracy cost that is acceptable for short support utterances. Accuracy and latency for this deployment have not been benchmarked in this repository.

### ADR-004: Ingress Concurrency — Full-Duplex WebSockets vs. HTTP Polling / SSE
* **Context:** Continuous bidirectional interaction: user audio/text in, streamed tokens and audio out.
* **Decision:** Full-duplex **WebSocket transport** for the console; Server-Sent Events kept for simple request/response API clients.
* **Trade-off Analysis:** Polling adds connection overhead and latency. A persistent WebSocket carries both directions on one connection; worker results reach the right socket through a per-session Redis Pub/Sub channel, so any gateway replica can serve the session's ingress while workers scale independently.

---

## 💥 Chaos Engineering & Failure Recovery

| Failure Scenario | Detection Mechanism | Automated Recovery Strategy | Client Impact |
| :--- | :--- | :--- | :--- |
| **Worker Crash Mid-Task** | Supervisor sees an entry idle `> 15s` in the consumer group's PEL (`XPENDING … IDLE`) | `XCLAIM` + re-enqueue with an incremented attempt count; any live worker picks it up | Answer delayed by ~15–20s |
| **Poison Payload** | Task has used 3 attempts (worker retry counter or supervisor attempt count) | Moved to `stream:dead:letter:queue` with error and origin stream | Client receives an `error` frame |
| **LLM Worker / Database Outage** | REST worker timeouts or router exceptions recorded by the circuit breaker | Breaker opens (≥50% failures), returns a fallback for 15s, then admits one canary probe | Fallback message instead of a hang |
| **Redis Outage** | Redis commands raise `ConnectionError` | Breaker and rate limiter fail open and log; cache lookups degrade to misses | Console keeps answering; REST SSE returns the fallback and voice an `error` frame (both need Redis) |
| **Traffic Spike** | Redis sliding-window log per user / IP | `429 Too Many Requests` + `Retry-After: 60` (REST) or an `error` frame (WebSocket) | Excess requests rejected early |

---

## ⚠️ Known Limitations
* **No generative LLM:** every answer comes from the deterministic intent router (tools, canned policy texts, pgvector retrieval). The "LLM worker" name reflects its place in the pipeline, not a language model.
* **Web console voice:** microphone input uses the browser's Web Speech API (Chrome, Edge, Safari); the console does not yet send `audio_chunk` frames or play `audio_response` audio from the TTS worker.
* **Schema changes:** tables are created with `create_all` on start-up; there are no migrations, so changing a model requires migrating existing databases by hand.
* **Observability:** Grafana ships without dashboards; circuit-breaker state lives in Redis (`circuit:ai_worker:state`) rather than a Prometheus metric.
* **Kubernetes:** the `k8s/` manifests are illustrative and have not been applied to a cluster; they expect pre-built `voxa/gateway` and `voxa/worker` images and a `voxa-secrets` Secret created out-of-band.
* **Frontend framework:** pinned to the latest Next.js 14.2 patch. Some Next.js advisories are only fixed in 15.5+/16, which is a major upgrade.

---

## 📄 License
Distributed under the **MIT License**. See [`LICENSE`](LICENSE) for details.

---

<div align="center">
<sub>Engineered by <b>Pratyush Kandari</b> • Distributed Voice & Real-Time AI Systems</sub>
</div>
