# SRE Incident Response Runbook & Operations Manual

This runbook defines triage workflows and mitigation steps for the Voxa platform as deployed with `docker-compose.yml` (container names `voxa-*`). Commands assume `redis-cli` and `psql` are run inside the corresponding containers, e.g. `docker exec -it voxa-redis redis-cli`.

---

## 1. Service-Level Objectives (SLOs) & Targets

| Metric | Target | Where to read it |
| :--- | :--- | :--- |
| **Availability** | $\ge 99.9\%$ (30-day rolling) | `/health` probe on the gateway |
| **TTFT (P95)** | $\le 180\text{ms}$ (5-minute rolling) | `llm_time_to_first_token_seconds` histogram; per-turn `ttft_ms` |
| **End-to-end latency (P95)** | Tracked, no SLO yet | `p95_latency_ms` in `/v1/admin/metrics` |
| **Queue backlog** | $\le 100$ tasks | `queue_depth` in `/v1/admin/metrics` (pending + lag) |
| **Circuit breaker** | State == `CLOSED` | Redis key `circuit:ai_worker:state` |

---

## 2. Incident Playbooks

### Playbook 1: Queue Backlog Growing
**Symptoms:** `queue_depth` rising in the operations console; voice or SSE answers slow down or time out.
**Triage:**
1. Inspect each consumer group (pending entries and `lag`):
   ```bash
   redis-cli XINFO GROUPS stream:audio:transcribe
   redis-cli XINFO GROUPS stream:llm:inference
   redis-cli XPENDING stream:llm:inference llm_group
   ```
2. Confirm workers are alive (heartbeat keys expire after 10s) and check resources:
   ```bash
   redis-cli --scan --pattern 'worker:heartbeat:*'
   docker stats voxa-stt-worker voxa-llm-worker
   ```
**Mitigation:**
1. Scale workers horizontally. Remove `container_name` from the service in `docker-compose.yml` first (Compose cannot scale services with a fixed container name):
   ```bash
   docker compose up -d --scale stt-worker=3 --scale llm-worker=3
   ```
2. If tasks are stuck in a PEL, `voxa-worker-supervisor` reclaims entries idle for more than 15s; make sure it is running (`docker logs voxa-worker-supervisor`). Inspect quarantined tasks:
   ```bash
   redis-cli XRANGE stream:dead:letter:queue - + COUNT 10
   ```

---

### Playbook 2: Circuit Breaker Tripped (`State == OPEN`)
**Symptoms:** Clients receive the "experiencing unusually high demand" fallback; WebSocket final frames report `"circuit_breaker": "OPEN"`.
**Triage:**
1. Check the breaker state and recent counters:
   ```bash
   redis-cli MGET circuit:ai_worker:state circuit:ai_worker:failures circuit:ai_worker:total
   ```
2. Look for the underlying errors (`Error executing agent query` or `LLM dispatch/relay failed`):
   ```bash
   docker logs --since 10m voxa-gateway | grep -iE "error|failed"
   ```
3. Check dependencies:
   ```bash
   docker exec -it voxa-postgres pg_isready -U postgres -d voxa_db
   docker exec -it voxa-redis redis-cli INFO memory
   docker logs --since 10m voxa-llm-worker
   ```
**Mitigation:**
1. Fix the failing dependency (restart the LLM worker, restore PostgreSQL). The breaker moves to `HALF_OPEN` 15s after tripping and closes automatically after one successful canary request.
   If final frames report `"circuit_breaker": "UNKNOWN"`, Redis itself is unreachable: the breaker and rate limiter are failing open (gateway logs show `Circuit breaker state unavailable` / `Rate limiter unavailable`), so restore Redis first.
2. To force a probe immediately:
   ```bash
   redis-cli SET circuit:ai_worker:state HALF_OPEN
   ```

---

### Playbook 3: Latency Degradation
**Symptoms:** `http_request_duration_seconds` or `llm_time_to_first_token_seconds` shift to higher buckets; HUD TTFT rises.
**Triage:**
1. Check the semantic cache hit rate:
   ```promql
   rate(semantic_cache_hit_total[5m])
     / (rate(semantic_cache_hit_total[5m]) + rate(semantic_cache_miss_total[5m]))
   ```
2. Check per-route latency:
   ```promql
   histogram_quantile(0.95, sum by (le, endpoint) (rate(http_request_duration_seconds_bucket[5m])))
   ```
3. Verify CPU throttling on the gateway (embedding inference) and STT workers.
**Mitigation:**
1. Confirm the STT worker logs `Loading Faster-Whisper [...] with INT8 quantization`.
2. Add gateway replicas (one uvicorn worker each) behind the load balancer.

---

## 3. Disaster Recovery & Deployment
- **Database backup:** schedule `pg_dump` of `voxa_db` (or volume snapshots of `postgres_data`); Redis holds only transient state (streams, cache, rate limits) with AOF enabled.
- **Secrets:** set `JWT_SECRET_KEY` (shared by all gateway replicas) and `SEED_ADMIN_PASSWORD` before exposing the stack; in Kubernetes create `voxa-secrets` out-of-band (see `k8s/configmap-secrets.yaml`). Rotating `JWT_SECRET_KEY` invalidates every issued token.
- **Admin password:** `SEED_ADMIN_PASSWORD` is used only when `admin@voxa.ai` is first created. To change it on a running stack:
  ```bash
  HASH=$(docker exec voxa-gateway python -c "from app.auth import get_password_hash; print(get_password_hash('<new-password>'))")
  docker exec -i voxa-postgres psql -U postgres -d voxa_db -c "UPDATE users SET hashed_password='$HASH' WHERE email='admin@voxa.ai';"
  ```
- **Network exposure:** Compose publishes only the frontend (3000) and gateway (8088). PostgreSQL, Redis, Prometheus and Grafana are bound to `127.0.0.1`; on a cloud VM reach them with an SSH tunnel and keep the VM's firewall closed for those ports.
- **Rolling update (Kubernetes):**
  ```bash
  kubectl apply -f voxa-backend/k8s/
  kubectl rollout status deployment/voxa-gateway
  ```
  The HPA keeps 2–10 `voxa-llm-worker` replicas, scaling up at 75% average CPU utilisation.
