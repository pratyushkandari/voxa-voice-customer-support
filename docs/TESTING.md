# Testing

How Voxa is tested, and what the last full run on 2026-10-29 measured. The commands are in the [README](../README.md#testing).

## Layers

| Layer | Where | Needs | Runs in CI |
| --- | --- | --- | --- |
| Unit tests (77) | `voxa-backend/tests/test_*.py` | nothing: Gemini, Redis, PostgreSQL and the models are replaced | yes |
| Migrations | `alembic` against a pgvector PostgreSQL | a database | yes |
| Frontend | lint, typecheck, production build | Node 20 | yes |
| End-to-end tests (17) | `tests/test_e2e_stack.py` | the full Compose stack | no (needs the models and edge-tts) |
| Voice, browser, failure and load runs | run by hand against the Compose stack | the full stack, Chrome, k6 | no |

The cases worth repeating on every change are in the unit and end-to-end suites. The larger runs below (48 voice clips, outages, 500 virtual users) take minutes and change the stack's state, so they're run before a release rather than on every push.

## What the end-to-end suite covers

- **Orders:** each demo order; `ord88412`, `ORD 88412`, "order number 88412", "Order #88412"; two orders in one message; an unknown order; IDs that are too short or missing.
- **Refunds:** inside and outside the 30-day window, an order that hasn't shipped, an unknown order.
- **Tickets:** "agent", "manager", "specialist", complaints, chargebacks and legal threats (urgent), and "I have an issue" (no ticket).
- **Knowledge base:** questions that only match an article's title ("bulk discounts").
- **Odd input:** empty, whitespace, one character, 2,000 and 2,001 characters, Hindi, Hinglish, emoji, SQL-like text.
- **Prompt injection:** six phrasings, each refused without a Gemini call.
- **Protocol:** malformed JSON, a JSON array, an unknown type, a binary frame, an `audio_chunk` without audio; the socket stays open after each.
- **Limits and auth:** the WebSocket and REST rate limits, signup validation, CORS, admin access as anonymous, guest and admin.
- **Voice:** a full round trip (WAV → transcript → reply → MP3), an order ID that Whisper splits ("Order-O or D10294"), silence, non-audio bytes, invalid base64 and an oversized recording.

## Results

### Answers (no Gemini key, so every reply is the router's plain reply)

| Check | Result |
| --- | --- |
| Knowledge base: right article first, 12 articles × (question + paraphrase) | 21/24 (18/24 before titles were embedded) |
| Right article among the 3 passages Gemini gets | 21/24 |
| Highest similarity for off-topic questions ("weather in Delhi", "hello") | 0.12, well under the 0.35 cut-off |
| Knowledge-base reply, first token | p50 25 ms, p95 36 ms |
| Order reply, first token | about 12 ms |

The three misses are paraphrases that share few words with their article: "My headphones stopped working" and "I went swimming with my earbuds" both match the product guide, and "Which is better for the gym?" matches nothing.

### Voice

48 recordings: 6 edge-tts voices (US, UK and Indian English, male and female) × 4 questions, each clean and with white noise at 10 dB SNR. Clips were 2–5 s long. The order and refund questions spell the ID out ("O R D 8 8 4 1 2").

| | Clean | Noise at 10 dB |
| --- | --- | --- |
| Correct reply | 24/24 | 24/24 |
| Word error rate | 3.8% | 2.6% |
| Order ID transcribed exactly | 11/12 | 12/12 |

The one inexact ID was "0RD-10294" (a zero), which the router accepts. Before two fixes in this run, 22/24 and 21/24 replies were correct: Whisper split IDs as "or D10294", and with noise it transcribed one Indian-English speaker in Malay.

Timings from sending the recording, p50 / p95:

| Step | p50 | p95 |
| --- | --- | --- |
| Transcription (Whisper base, int8, CPU) | 0.86 s | 1.43 s |
| Transcript shown | 0.93 s | 1.46 s |
| First reply text | 0.94 s | 1.47 s |
| Speech synthesis (edge-tts) | 1.58 s | 2.13 s |
| Spoken reply starts | 2.55 s | 3.31 s |

A 1 s clip ("Refund.") was transcribed in 0.8 s and a 28 s clip in 2.1 s. A spoken phone number was masked in the transcript. Silence and noise-only recordings get "No speech was detected".

### Failures

| Scenario | Result |
| --- | --- |
| Invalid Gemini key | Every reply is the plain reply; the breaker opens after 4 failed calls, lets one probe through after 15 s, and re-opens when it fails. The 400 error is logged. |
| LLM worker crashes after taking a task | The supervisor re-queues it; the reply arrived 19–20 s after the worker restarted. |
| Redis stopped | Typed questions are still answered; voice gets "Voice pipeline is unavailable"; voice works again once Redis is back. |
| PostgreSQL stopped | Order questions get an error frame after about 8 s; injection refusals still work; answers resume once it's back. |
| Gateway restarted with a fixed `JWT_SECRET_KEY` | Tokens issued before the restart still work and conversations are still saved. |
| edge-tts unreachable | The text reply still arrives, with no audio; the failure is logged. |
| PII | Card numbers, phone numbers (US and Indian formats) and emails are masked in saved messages; none appear in the gateway or STT logs. |
| Someone else's order | Another user, a guest claiming to be admin and an anonymous socket all get the same reply as for a missing order. |

### Browser (headless Chrome)

17/17: the landing page and gateway status; the four quick actions; the Fallback badge; HTML in a message shown as text; voice with a fake microphone (Transcribing… → transcript → reply → audio plays); admin sign-in, metrics and tickets; a wrong admin password; no sideways scrolling at 375 px; a blocked microphone; the offline banner and automatic reconnect; and no console errors.

### Load

`tests/load_test_websockets.js`: each virtual user opens a WebSocket, asks a refund question, waits for the reply and closes. No Gemini key, rate limit raised, one gateway process.

| Virtual users | Sessions per second | Connect p95 | Session p95 | Errors |
| --- | --- | --- | --- | --- |
| 100 (steady) | 80 | 38 ms | 238 ms | 0 |
| 200 (steady) | 105 | 801 ms | 1.1 s | 0 |
| ramp to 500 (the script's stages) | 108 | 2.0 s | 2.2 s | 0 |

One gateway process handles about 100 new sessions a second. Beyond that, sessions queue and connect time grows, so the script's `ws_connecting p(95)<200` threshold fails at 500 users. Every session still got its answer. Gateway replicas behind a load balancer are the way to scale (see the runbook).

### Migrations

On an empty database: upgrade to `0003`, downgrade to empty, upgrade again, then `alembic check` (no differences from the models). On a database created by the old start-up code: stamped at `0001`, upgraded to `0003`, existing orders kept.

## Not covered

- A real microphone: the browser checks play a recorded clip through a stubbed microphone.
- More than one gateway replica.
- Accents and noise beyond the six synthetic voices and white noise above.
