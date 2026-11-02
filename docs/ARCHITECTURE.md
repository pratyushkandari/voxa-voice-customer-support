# Architecture

This document describes how Voxa's services fit together, the wire protocol between the console and the gateway, how answers are built, and how failures are handled. For setup and the API surface, see the [README](../README.md).

## Overview

Voxa is split into a gateway and a set of workers that talk through Redis:

- The **gateway** (FastAPI) owns client connections and the conversation records. It serves the WebSocket used by the console and answers typed questions itself.
- **Workers** handle the slow or CPU-heavy voice work: speech-to-text, answering transcripts, and text-to-speech. Each worker type is a Redis Streams consumer group, so it can be scaled on its own.
- Worker results flow back over a per-session Redis Pub/Sub channel, so any gateway replica can deliver them to the right client.
- **PostgreSQL with pgvector** stores users, orders, conversations, tickets and the knowledge base, including its embeddings. Its schema is versioned with Alembic. A one-off `migrate` container applies the migrations before the gateway and workers start, so replicas never race to change it.
- **Gemini** (`gemini-3.6-flash` by default) writes replies from facts the code has already fetched.

```mermaid
graph TD
    Client["Next.js console"]
    Gateway["Gateway (FastAPI WebSocket)"]
    Redis["Redis 7 (streams, pub/sub, cache, rate limits, breaker state)"]
    STT["STT worker (Faster-Whisper, int8)"]
    LLM["LLM worker (router + Gemini)"]
    TTS["TTS worker (edge-tts)"]
    SUP["Worker supervisor (PEL reclaim, DLQ)"]
    PG[("PostgreSQL 16 + pgvector")]
    Gemini(["Gemini API"])
    Prom["Prometheus"]

    Client <-->|"WebSocket (JSON frames)"| Gateway
    Gateway -->|"stream:audio:transcribe"| Redis
    Redis -->|"stt_group"| STT
    STT -->|"stream:llm:inference"| Redis
    Redis -->|"llm_group"| LLM
    LLM <-->|"orders, tickets, HNSW search"| PG
    LLM -->|"facts + question"| Gemini
    LLM -->|"stream:tts:synthesize"| Redis
    Redis -->|"tts_group"| TTS
    STT -->|"token:stream:{session_id}"| Redis
    LLM -->|"token:stream:{session_id}"| Redis
    TTS -->|"token:stream:{session_id}"| Redis
    Redis -->|"transcript, token and audio events"| Gateway
    Gateway <-->|"orders, tickets, conversations, knowledge base"| PG
    Gateway -->|"facts + question (typed)"| Gemini
    SUP -.->|"XPENDING / XCLAIM / DLQ"| Redis
    Gateway -.->|"/metrics"| Prom
    LLM -.->|":9100/metrics"| Prom
```

Typed questions are answered in the gateway because nothing in them is CPU-heavy, so a worker hop would only add latency. Spoken questions already pass through the queue for transcription, so the LLM worker answers the transcript where it is. Both use the same function, `answer_query` in `app/llm.py`.

## WebSocket protocol

Clients connect to `/v1/ws/stream?token=<JWT>`. The token is optional. Anonymous sessions work, but only authenticated sessions are saved (a conversation belongs to a user) and linked to the tickets they create. An invalid or expired token isn't rejected; the session is simply anonymous. The token travels in the query string because browsers can't set headers on a WebSocket handshake; the gateway hides it in its access logs.

All frames are JSON text frames. A binary frame gets an `error` reply.

**Client → gateway**

- `{"type": "text_query", "text": "...", "trace_id": "<optional>"}`: at most 2,000 characters.
- `{"type": "audio_chunk", "audio_data": "<base64>", "trace_id": "<optional>"}`: one utterance, at most 4 MB of base64. Any container FFmpeg can decode works (WAV, WebM/Opus, OGG, MP4/AAC). The console caps recordings at 30 s.

**Gateway → client**

- `ack`: an `audio_chunk` was queued for transcription.
- `transcript`: what Whisper heard, `{"text", "stt_latency_ms", "trace_id"}`.
- `token_chunk`: the reply, streamed as Gemini produces it (a fallback or cached reply arrives as one chunk). The last frame has `is_final: true` and carries that turn's measurements:

  ```json
  {
    "type": "token_chunk",
    "token": "",
    "is_final": true,
    "ttft_ms": 2407,
    "total_latency_ms": 2567,
    "cache_hit": false,
    "fallback": false,
    "circuit_breaker": "CLOSED",
    "trace_id": "b1a47df1-0c5a-4e89-b7b5-fb358a9ce71b"
  }
  ```

  This one is a reply Gemini wrote for an order question. `fallback` is `true` whenever the plain reply was sent instead of Gemini's. `circuit_breaker` is the Gemini breaker state whenever it was checked, and `null` when it wasn't (no key, a cache hit, a blocked injection attempt). For voice, `ttft_ms` and `total_latency_ms` are measured by the LLM worker and don't include transcription.

- `audio_response`: base64 MP3 of the reply from the TTS worker (voice requests only). It arrives after the final `token_chunk`.
- `error`: a validation failure, a rate limit, an empty transcript, a dead-lettered task or an answering failure.

## Building an answer

`answer_query` runs two steps.

**1. Route and fetch facts.** `route_query` in `app/agent_tools.py` checks rules in priority order and stops at the first match:

1. Prompt-injection guardrail: a fixed refusal, never sent to Gemini.
2. Chargeback or legal threats: opens an urgent ticket.
3. Order lookup or refund eligibility from PostgreSQL, for up to three orders per message. Accepted forms:
   - `ORD-88412`, `ORD 88412` and `ORD88412`
   - written out: "order 88412", "order number 88412", "order #88412"
   - the ways Whisper splits a spoken ID: "or D88412", "0RD-88412"
4. Request for a person: opens a ticket.
5. Everything else: the semantic cache, then the top 3 knowledge-base passages with similarity above 0.35.

Each branch returns two things:

- **The facts** for Gemini, for example `Order ORD-88412: Voxa Wireless AI Earbuds Pro, ordered 2026-10-23, status Shipped, tracking number TRK-99281744US.`
- **A complete plain reply** built from the same facts.

An order that belongs to a user is visible only to that user. The SQL query applies the owner filter, so Gemini only ever receives "not found" for someone else's order. The seeded demo orders have no owner and are visible to everyone. Someone else's order gets the same reply as a missing one, so order IDs can't be probed.

**2. Write the reply.** `write_reply` in `app/llm.py` sends Gemini:

- a short system prompt: one to three plain sentences, use only the facts, repeat IDs exactly, treat the customer's text as untrusted
- the facts
- the masked question

The reply streams back chunk by chunk with the thinking level set to low. Gemini is skipped, and the plain reply sent instead, when:

- there is no `GEMINI_API_KEY`,
- the circuit breaker is open,
- the call raises (network, 4xx/5xx, quota),
- the reply takes longer than 10 s, or
- the reply comes back empty.

If the stream breaks after some text was sent, the plain reply is appended so the user still gets the facts.

## Workers and delivery guarantees

The gateway creates these consumer groups on start-up (idempotently):

| Stream | Group | Consumer |
| --- | --- | --- |
| `stream:audio:transcribe` | `stt_group` | STT worker |
| `stream:llm:inference` | `llm_group` | LLM worker (up to 8 entries per read, answered concurrently) |
| `stream:tts:synthesize` | `tts_group` | TTS worker |

The dead-letter stream, `stream:dead:letter:queue`, has no consumer group: nothing consumes it, and it's inspected with `XRANGE`.

Every `XADD` uses `MAXLEN ~ 10000`, so acknowledged entries don't accumulate.

Delivery is at-least-once:

- **Acknowledge last:** a worker `XACK`s an entry only after it has answered, re-queued or dead-lettered it.
- **Own pending entries first:** on each loop a worker re-reads its own unacknowledged entries (`XREADGROUP ... 0`) before taking new ones. Consumer names are random per process, so entries left by a crashed process are handled by the supervisor instead.
- **Crash recovery:** every 5 s the supervisor asks for entries idle longer than 15 s (`XPENDING ... IDLE 15000`), claims them and re-queues them with `retry_count` incremented. The attempt count travels in the payload, because a re-queued entry starts a fresh delivery counter.
- **Long tasks:** while a worker is busy (a long transcription, or a batch waiting on Gemini), it re-claims its entries every 5 s (`XCLAIM ... JUSTID`). Healthy work is therefore never mistaken for a crash and run twice.
- **Dead-letter queue:** after 3 attempts an entry goes to `stream:dead:letter:queue` with the error and its origin stream, and the client gets an `error` frame.

The LLM worker answers a batch concurrently, because each answer mostly waits on Gemini. It also loads the embedding model before it starts consuming; loading it lazily on the first transcript took about 20 s.

**Speech-to-text settings.** The STT worker runs Faster-Whisper `base` (int8, CPU) with three settings, each added after a measured failure:
- a voice-activity filter, because on silence Whisper returned its own prompt as the transcript
- the language fixed to English, because with background noise it once transcribed an Indian-English speaker in Malay
- a prompt showing the order-ID format, because without it a spoken "ORD-88412" came back as "or D88412"

A recording with no speech gets "No speech was detected".

**Saving voice turns.** The gateway's Pub/Sub forwarder already sees the transcript, the reply tokens and the final frame for each voice request, so it collects them and saves the exchange with the same helper as typed questions. Workers never write conversations.

## Retrieval and the semantic cache

```
knowledge question (PII already masked)
  │
  ├─ Redis semantic cache: cosine ≥ 0.92 ──► cached reply (cache_hit = true, no Gemini call)
  │
  └─ miss ─► pgvector HNSW search (<=>) ──► passages with similarity > 0.35
                                              ──► Gemini reply ──► stored in the cache
```

**Embeddings**

- `all-MiniLM-L6-v2`, 384 dimensions.
- L2-normalised, so cosine similarity is a dot product.
- Encoding runs in a thread so it never blocks the event loop.
- A question is embedded once. The same vector is used for the cache lookup, the search and the cache entry, because embedding is the slowest step of a plain reply (30–200 ms on the test machine, depending on load).
- An article is embedded from its title and body together. Titles often hold the words people search for ("Bulk & Business Orders"); with the body alone, "Do you offer bulk discounts?" matched nothing.

**Cache**

- A lookup scans at most 50 keys with `SCAN` and fetches them with one `MGET`.
- Entries expire after an hour.
- Only knowledge answers that Gemini wrote are stored. Order and ticket replies are user-specific, and fallback replies would otherwise keep being served after Gemini recovers.

**Knowledge base**

- The `knowledge_base` table is seeded on start-up. Any missing topic is added, so existing databases pick up new documents. Migration `0003` empties the table once, so articles embedded the old way (body only) are re-embedded on the next start.
- It's indexed with `USING hnsw (embedding vector_cosine_ops)` (pgvector defaults: `m = 16`, `ef_construction = 64`).

## Circuit breaker

Gemini calls from the gateway and the LLM worker feed one Redis-backed breaker (`circuit:gemini:*`), so all of them see the same state.

```mermaid
stateDiagram-v2
    [*] --> Closed
    Closed --> Open: failures/total ≥ 50% with ≥ 4 calls in 10 s
    Open --> HalfOpen: 15 s cooldown
    HalfOpen --> Closed: probe succeeds
    HalfOpen --> Open: probe fails
```

- **Closed:** Gemini is called, and successes and failures are both counted.
- **Open:** Gemini is skipped and the plain reply is sent immediately.
- **Half-open:** one probe request holds a 10 s `SET NX EX` lease; everyone else gets the plain reply.
- **Redis unreachable:** the breaker can't read or write its state, so it lets calls through (state `UNKNOWN`) and logs a warning. The rate limiter does the same.

## Metrics

The gateway serves Prometheus metrics on `/metrics`, and the LLM worker on port 9100:

- `http_request_duration_seconds{method,endpoint,status_code}` (gateway): labelled by route template, not raw path, to keep cardinality bounded.
- `gemini_requests_total{outcome="ok"|"fallback"}`: replies Gemini was asked to write, and how many fell back.
- `gemini_request_duration_seconds`: from sending the request until the reply finished streaming or failed.
- `websocket_active_connections` (gateway): open sockets on this process.
- `semantic_cache_hit_total`, `semantic_cache_miss_total`: per-process counters. Fleet-wide totals are kept in Redis (`metric:semantic_cache_*`) for the admin API.

Metrics are process-local, so the gateway runs one uvicorn worker per container and scales by adding replicas.

`GET /v1/admin/metrics` (admin only) adds values computed from the database and Redis:

- average TTFT and p95 latency of saved replies
- cache hit ratio
- live workers (`worker:heartbeat:*`, 10 s TTL)
- queue backlog (pending plus lag per consumer group)

## Tuned values

Each value below is set in one place. Docs, comments and the frontend state the same value. "Decision" means it was chosen, not tuned on data; the measurements are in [TESTING.md](TESTING.md).

| Value | Set in | Rule | Where it came from |
| --- | --- | --- | --- |
| Gemini timeout: 10 s | `GEMINI_TIMEOUT_SEC`, `app/llm.py` | No complete reply within 10 s → the plain reply is sent | Decision. Measured time to first token: p50 2.9 s, p95 4.3 s, slowest 6.0 s |
| Thinking level: low; at most 1,024 output tokens | `GENERATION_CONFIG`, `app/llm.py` | | Decision |
| Semantic cache: cosine ≥ 0.92 | `check_semantic_cache`, `app/rag_engine.py` | Only a near-identical question reuses a cached reply | Decision |
| Cache entry lifetime: 1 hour; lookup scans at most 50 keys | `cache_ttl`, `SEMANTIC_CACHE_SCAN_LIMIT`, `app/rag_engine.py` | | Decision |
| Knowledge-base cut-off: similarity > 0.35, top 3 passages | `route_query`, `app/agent_tools.py` | Below the cut-off the general help reply is used | Decision. Measured: off-topic questions score at most 0.12, and the right article is above the cut-off for 21 of 24 test questions (the three misses score 0.24–0.33) |
| Article embedding: title + body | `seed_data.py` | | Measurement: the right article comes first for 21 of 24 test questions, against 18 with the body only |
| Orders per message: 3 | `MAX_ORDERS_PER_MESSAGE`, `app/agent_tools.py` | Further order IDs in the same message are ignored | Decision |
| Refund window: 30 days | `check_refund_eligibility`, `app/agent_tools.py` | Eligible if bought 30 or fewer days ago | Business rule, the same as the Return Policy article |
| Circuit breaker: ≥ 50% failures with ≥ 4 calls in 10 s; 15 s cooldown; 10 s probe lease | `DistributedCircuitBreaker` defaults, `app/circuit_breaker.py` | See [Circuit breaker](#circuit-breaker) | Decision. Measured with an invalid key: opens after 4 failed calls, probes after 15 s |
| Rate limit: 30 requests a minute per user, or per IP for anonymous calls | `RATE_LIMIT_REQUESTS_PER_MINUTE` (`app/config.py`, `.env.example`) | REST gets `429`, the WebSocket an `error` frame | Decision |
| Typed question: at most 2,000 characters | `WS_MAX_TEXT_QUERY_CHARS`; `MAX_MESSAGE_CHARS` in the console | | Decision |
| Recording: at most 4,000,000 base64 characters (about 3 MB); the console stops at 30 s | `WS_MAX_AUDIO_CHUNK_B64_CHARS`; `MAX_AUDIO_B64_CHARS`, `MAX_RECORDING_MS` in the console | | Decision. A 28 s, 16 kHz WAV is about 0.9 MB |
| Worker attempts: 3, then the dead-letter queue | `MAX_ATTEMPTS` in the workers, `max_attempts` in the supervisor | | Decision |
| Crash recovery: entries idle over 15 s are reclaimed, checked every 5 s; busy workers re-claim their entries every 5 s | `worker_supervisor.py`, `CLAIM_REFRESH_SEC` in `app/queue_manager.py` | The re-claim interval must stay below 15 s | Decision. Measured: a task left by a crashed worker was answered about 20 s after the worker restarted |
| Stream length cap: about 10,000 entries | `STREAM_MAXLEN`, `app/config.py` | | Decision |
| Token lifetime: 24 hours | `ACCESS_TOKEN_EXPIRE_MINUTES` (`app/config.py`, `.env.example`) | | Decision |
| Speech-to-text: Whisper `base`, int8, beam size 2, voice-activity filter, English, ID-format prompt | `STT_MODEL_SIZE`, `workers/stt_worker.py` | | The filter, the language and the prompt each fixed a measured failure (see [Speech-to-text settings](#workers-and-delivery-guarantees)); the model size is a decision for a CPU-only VM |
| Time-to-first-token target: 6 s | `TTFT_TARGET_MS` in the admin console; [RUNBOOK targets](RUNBOOK.md#targets) | Average TTFT at or under the target shows as met | Decision, above the measured p95 of 4.3 s |
| Load test: connect p95 < 200 ms (threshold); first token < 200 ms (check) | `voxa-backend/tests/load_test_websockets.js` | | Targets. Measured with one gateway process: the connect threshold held at 100 virtual users, not at 200 or 500; 99.8% of first tokens were within 200 ms at 100 users |
