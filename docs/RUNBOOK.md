# Operations runbook

How to diagnose and fix the common problems in a Voxa deployment run with `docker-compose.yml` (containers are named `voxa-*`). Run `redis-cli` and `psql` inside their containers, e.g. `docker exec -it voxa-redis redis-cli`.

## Targets

| What | Target | Where to look |
| --- | --- | --- |
| Availability | 99.9% over 30 days | gateway `/health` |
| Time to first token (p95) | ≤ 180 ms | `llm_time_to_first_token_seconds`, per-answer `ttft_ms` |
| End-to-end latency (p95) | tracked, no target yet | `p95_latency_ms` in `/v1/admin/metrics` |
| Queue backlog | ≤ 100 tasks | `queue_depth` in `/v1/admin/metrics` (pending + lag) |
| Circuit breaker | `CLOSED` | Redis key `circuit:ai_worker:state` |

## Queue backlog growing

**Symptoms:** `queue_depth` keeps rising in the admin console, and voice or REST answers slow down or time out.

1. Look at each consumer group:
   ```bash
   redis-cli XINFO GROUPS stream:audio:transcribe
   redis-cli XINFO GROUPS stream:llm:inference
   redis-cli XPENDING stream:llm:inference llm_group
   ```
2. Check the workers are alive (heartbeat keys expire after 10 s) and not starved of CPU or memory:
   ```bash
   redis-cli --scan --pattern 'worker:heartbeat:*'
   docker stats voxa-stt-worker voxa-llm-worker
   ```
3. Add workers. Compose can't scale a service that has a fixed `container_name`, so remove that line from the service first, then:
   ```bash
   docker compose up -d --scale stt-worker=3 --scale llm-worker=3
   ```
4. Entries stuck in a pending list are reclaimed by `voxa-worker-supervisor` once they've been idle for 15 s, so make sure it's running (`docker logs voxa-worker-supervisor`). To see what was dead-lettered:
   ```bash
   redis-cli XRANGE stream:dead:letter:queue - + COUNT 10
   ```

## Circuit breaker open

**Symptoms:** users get the "experiencing unusually high demand" fallback, and final WebSocket frames report `"circuit_breaker": "OPEN"`.

1. Check the breaker state and its counters:
   ```bash
   redis-cli MGET circuit:ai_worker:state circuit:ai_worker:failures circuit:ai_worker:total
   ```
2. Find the errors behind the trips (`Error executing agent query` or `LLM dispatch/relay failed`):
   ```bash
   docker logs --since 10m voxa-gateway | grep -iE "error|failed"
   ```
3. Check the dependencies:
   ```bash
   docker exec -it voxa-postgres pg_isready -U postgres -d voxa_db
   docker exec -it voxa-redis redis-cli INFO memory
   docker logs --since 10m voxa-llm-worker
   ```
4. Fix whatever is failing, e.g. restart the LLM worker or bring PostgreSQL back. The breaker moves to `HALF_OPEN` 15 s after it trips and closes after one successful probe. To probe right away:
   ```bash
   redis-cli SET circuit:ai_worker:state HALF_OPEN
   ```

If final frames report `"circuit_breaker": "UNKNOWN"`, Redis itself is unreachable. The breaker and the rate limiter are letting requests through, and the gateway logs `Circuit breaker state unavailable` / `Rate limiter unavailable`. Restore Redis first.

## Latency getting worse

**Symptoms:** `http_request_duration_seconds` or `llm_time_to_first_token_seconds` move into higher buckets, and TTFT rises in the console.

1. Check the semantic cache hit rate:
   ```promql
   rate(semantic_cache_hit_total[5m])
     / (rate(semantic_cache_hit_total[5m]) + rate(semantic_cache_miss_total[5m]))
   ```
2. Check latency per route:
   ```promql
   histogram_quantile(0.95, sum by (le, endpoint) (rate(http_request_duration_seconds_bucket[5m])))
   ```
3. Look for CPU throttling on the gateway (it computes embeddings) and on the STT workers.
4. Confirm the STT worker logged `Loading Faster-Whisper [...] with INT8 quantization` at start-up.
5. Add gateway replicas (one uvicorn worker each) behind the load balancer.

## Backups, secrets and deployment

- **Backups:** run `pg_dump` on `voxa_db` regularly (or snapshot the `postgres_data` volume). Redis only holds short-lived state (streams, cache, rate limits) and has AOF turned on.
- **Secrets:** set `JWT_SECRET_KEY` (the same value on every gateway replica) and `SEED_ADMIN_PASSWORD` before exposing the stack. On Kubernetes, create the `voxa-secrets` Secret separately (see `k8s/configmap-secrets.yaml`). Changing `JWT_SECRET_KEY` invalidates every token already issued.
- **Admin password:** `SEED_ADMIN_PASSWORD` only applies when `admin@voxa.ai` is first created. To change the password later:
  ```bash
  HASH=$(docker exec voxa-gateway python -c "from app.auth import get_password_hash; print(get_password_hash('<new-password>'))")
  docker exec -i voxa-postgres psql -U postgres -d voxa_db -c "UPDATE users SET hashed_password='$HASH' WHERE email='admin@voxa.ai';"
  ```
- **Network exposure:** Compose publishes only the frontend (3000) and the gateway (8088). PostgreSQL, Redis, Prometheus and Grafana listen on `127.0.0.1`. On a cloud VM, reach them through an SSH tunnel and keep those ports closed in the firewall.
- **Kubernetes rollout:**
  ```bash
  kubectl apply -f voxa-backend/k8s/
  kubectl rollout status deployment/voxa-gateway
  ```
  The HPA keeps between 2 and 10 `voxa-llm-worker` replicas and scales up at 75% average CPU.
