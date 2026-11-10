# Operations runbook

How to diagnose and fix the common problems in a Voxa deployment run with `docker-compose.yml` (containers are named `voxa-*`). Run `redis-cli` and `psql` inside their containers, e.g. `docker exec -it voxa-redis redis-cli`.

## Targets

| What | Target | Where to look |
| --- | --- | --- |
| Availability | 99.9% over 30 days | gateway `/health` |
| Time to first token (p95) | ≤ 6 s (Gemini replies measured p95 4.3 s) | per-answer `ttft_ms`, `avg_ttft_ms` in `/v1/admin/metrics` |
| Gemini fallback rate | ≤ 5% | `gemini_requests_total{outcome="fallback"}` |
| End-to-end latency (p95) | tracked, no target yet | `p95_latency_ms` in `/v1/admin/metrics` |
| Queue backlog | ≤ 100 tasks | `queue_depth` in `/v1/admin/metrics` (pending + lag) |
| Circuit breaker | `CLOSED` | Redis key `circuit:gemini:state` |

## Replies show "Fallback"

**Symptoms:** replies are short and plain, the console shows a "Fallback" badge, and the fallback rate rises.

1. Check the breaker and its counters:
   ```bash
   redis-cli MGET circuit:gemini:state circuit:gemini:failures circuit:gemini:total
   ```
2. Find the cause in the logs (`Gemini reply failed, sending the fallback reply: <error>`):
   ```bash
   docker logs --since 10m voxa-gateway | grep "Gemini reply failed"
   docker logs --since 10m voxa-llm-worker | grep "Gemini reply failed"
   ```
   - `ClientError: 429 RESOURCE_EXHAUSTED`: the quota is used up. The error's `quotaId` says which limit:
     - `...PerMinute...` clears within a minute.
     - `...PerDay...` resets at midnight Pacific time.
     - Free-tier keys were limited to 5 requests a minute and 20 a day per model on 2026-10-28.
     - Wait, point `GEMINI_MODEL` at another model (each has its own quota), or enable billing on the key's project.
   - `ClientError: 400` / `403`: a bad key or model name. Check `GEMINI_API_KEY` and `GEMINI_MODEL` in `.env`, then `docker compose up -d gateway llm-worker`.
   - `TimeoutError`: Gemini took longer than 10 s. Check the Gemini status page and `gemini_request_duration_seconds`.
3. If every reply is a fallback and there are no errors in the logs, the key isn't set:
   ```bash
   docker exec voxa-gateway python -c "from app.config import settings; print(bool(settings.GEMINI_API_KEY))"
   ```
4. The breaker moves to `HALF_OPEN` 15 s after it trips and closes after one successful probe. To probe right away:
   ```bash
   redis-cli SET circuit:gemini:state HALF_OPEN
   ```

If final frames report `"circuit_breaker": "UNKNOWN"`, Redis itself is unreachable. The breaker and the rate limiter are letting requests through, and the gateway logs `Circuit breaker state unavailable` / `Rate limiter unavailable`. Restore Redis first.

## Answers fail with an error

**Symptoms:** typed questions get "Sorry, something went wrong while answering".

The router couldn't fetch its facts, which almost always means PostgreSQL:

```bash
docker logs --since 10m voxa-gateway | grep "Could not answer"
docker exec -it voxa-postgres pg_isready -U postgres -d voxa_db
```

## Voice requests hang or fail

**Symptoms:** a voice message stays at "Transcribing…", or no spoken reply arrives.

1. Check the queues and workers (heartbeat keys expire after 10 s):
   ```bash
   redis-cli XINFO GROUPS stream:audio:transcribe
   redis-cli XINFO GROUPS stream:llm:inference
   redis-cli --scan --pattern 'worker:heartbeat:*'
   docker stats voxa-stt-worker voxa-llm-worker
   ```
2. No transcript: check `docker logs voxa-stt-worker` for `Transcribed session` lines or FFmpeg decode errors. "No speech was detected" means the voice-activity filter found no speech in the clip: usually a muted or very quiet microphone.
3. Text but no audio: the TTS worker needs outbound internet access for edge-tts. Check `docker logs voxa-tts-worker` for `TTS synthesis failed`.
4. Add workers. Compose can't scale a service that has a fixed `container_name`, so remove that line from the service first, then:
   ```bash
   docker compose up -d --scale stt-worker=3 --scale llm-worker=3
   ```
5. Entries stuck in a pending list are reclaimed by `voxa-worker-supervisor` once they've been idle for 15 s, so make sure it's running (`docker logs voxa-worker-supervisor`). To see what was dead-lettered:
   ```bash
   redis-cli XRANGE stream:dead:letter:queue - + COUNT 10
   ```

## Latency getting worse

**Symptoms:** TTFT rises in the console, or `gemini_request_duration_seconds` moves into higher buckets.

1. Split Gemini time from everything else:
   ```promql
   histogram_quantile(0.95, sum by (le) (rate(gemini_request_duration_seconds_bucket[5m])))
   histogram_quantile(0.95, sum by (le, endpoint) (rate(http_request_duration_seconds_bucket[5m])))
   ```
2. Check the semantic cache hit rate. Every hit is a Gemini call saved:
   ```promql
   sum(rate(semantic_cache_hit_total[5m]))
     / (sum(rate(semantic_cache_hit_total[5m])) + sum(rate(semantic_cache_miss_total[5m])))
   ```
3. Look for CPU throttling on the gateway (it computes embeddings) and on the STT workers.
4. Confirm the STT worker logged `Loading Faster-Whisper [...] with INT8 quantization` at start-up.
5. Add gateway replicas (one uvicorn worker each) behind the load balancer. One gateway process handled about 100 new sessions a second in the k6 test; past that, connect times grow (see [TESTING.md](TESTING.md#load)).

## Backups, secrets and deployment

- **Backups:** run `pg_dump` on `voxa_db` regularly (or snapshot the `postgres_data` volume). Redis only holds short-lived state (streams, cache, rate limits, breaker) and has AOF turned on.
- **Secrets:**
  - Set `JWT_SECRET_KEY` (the same value on every gateway replica), `SEED_ADMIN_PASSWORD` and `GEMINI_API_KEY` in the root `.env` before exposing the stack, and never commit that file.
  - Changing `JWT_SECRET_KEY` invalidates every token already issued.
- **Admin password:** `SEED_ADMIN_PASSWORD` only applies when `admin@voxa.ai` is first created. To change the password later:
  ```bash
  HASH=$(docker exec voxa-gateway python -c "from app.auth import get_password_hash; print(get_password_hash('<new-password>'))")
  docker exec -i voxa-postgres psql -U postgres -d voxa_db -c "UPDATE users SET hashed_password='$HASH' WHERE email='admin@voxa.ai';"
  ```
- **Network exposure:**
  - Compose publishes only the frontend (3000) and the gateway (8088).
  - PostgreSQL, Redis and Prometheus listen on `127.0.0.1`, and the LLM worker's metrics port is only reachable on the Compose network.
  - On a cloud VM, reach them through an SSH tunnel and keep those ports closed in the firewall.
  - Voice input on a public address needs HTTPS in front of the frontend and gateway.
- **Deploying:** `bash scripts/deploy.sh` from the repository root on the server. It:
  - takes a lock, so only one deploy runs at a time
  - resets the checkout to `origin/main`, and stops there if that commit is already deployed and healthy (`--force` rebuilds anyway)
  - saves a database backup to `~/backups` (the last 5 are kept)
  - stops the app containers and removes their images and the build cache, because a 30 GB disk can't hold the old and new images together
  - builds the images one at a time (4 GB of RAM can't build them in parallel)
  - starts everything, then waits for `/health`

  The app is down from the moment it stops until the gateway is healthy again. PostgreSQL and Redis keep running. If a build fails, the app stays stopped: fix the cause and run `bash scripts/deploy.sh --force`.
- **Automatic deploys:** the server checks `main` every 5 minutes and deploys any new commit. It needs no GitHub secrets. The cron entry (add it with `crontab -e`):
  ```
  */5 * * * * cd ~/voxa-voice-customer-support && git fetch -q origin main && git reset -q --hard origin/main && bash scripts/deploy.sh >> ~/deploy.log 2>&1
  ```
  - It deploys whatever is on `main` and doesn't wait for CI, so merge only pull requests whose checks pass.
  - `~/deploy.log` is only written to while a deploy runs; checks that find nothing new stay silent.
  - A commit that failed to deploy isn't retried. Push a fix, or run `bash scripts/deploy.sh --force`.
  - To stop automatic deploys, remove the line with `crontab -e`.
- **Upgrading by hand:** `bash scripts/deploy.sh`, or `git pull` then `docker compose up --build -d`.
  - The `voxa-migrate` container applies any new migrations before the gateway and LLM worker start. Check it with `docker logs voxa-migrate`; if it fails, they don't start.
  - New knowledge-base documents are added on gateway start; existing rows are left alone.
  - Migration `0003` empties the knowledge base so the gateway re-embeds every article (title and body) on its next start; this takes a few seconds. Questions answered in between get the general reply.
- **Rolling back a migration:** back up first, then `docker compose run --rm migrate alembic downgrade -1`. `docker compose run --rm migrate alembic current` shows the applied revision.
