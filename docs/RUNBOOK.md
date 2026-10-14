# SRE Incident Response Runbook & Operations Manual

This runbook defines triage workflows, mitigation protocols, and alert definitions for the Voxa Distributed Platform.

---

## 1. Service-Level Objectives (SLOs) & Targets

| Metric | Target | Measurement Window | Alert Severity |
| :--- | :--- | :--- | :--- |
| **Availability** | $\ge 99.9\%$ | 30-day rolling | P1 / Page |
| **Voice TTFT (P95)** | $\le 180\text{ms}$ | 5-minute rolling | P2 / Slack |
| **Voice TTFT (P99)** | $\le 250\text{ms}$ | 5-minute rolling | P2 / Slack |
| **Consumer Group Lag** | $\le 100\text{ tasks}$ | 2-minute rolling | P2 / Slack |
| **Circuit Breaker Status** | State == `CLOSED` | Instantaneous | P1 / Page |

---

## 2. Incident Playbooks

### Playbook 1: Consumer Group Lag Spike (`XPENDING` > 500)
**Symptoms:** Client audio latency increases; Telemetry HUD shows rising `queue_latency_ms`.
**Triage:**
1. Check stream pending count:
   ```bash
   redis-cli XPENDING stream:audio:ingress stt_group
   ```
2. Inspect worker container health and resource utilization:
   ```bash
   docker stats voxa-stt-worker voxa-llm-worker
   ```
**Mitigation:**
1. Horizontal scale workers:
   ```bash
   docker compose up -d --scale stt-worker=3 --scale llm-worker=3
   ```
2. If tasks are poisoned (stuck in PEL):
   The supervisor auto-claims idle messages. Inspect dead-letter queue:
   ```bash
   redis-cli XRANGE stream:dead:letter:queue - + COUNT 10
   ```

---

### Playbook 2: Circuit Breaker Tripped (`State == OPEN`)
**Symptoms:** Prometheus metric `voxa_circuit_breaker_state == 2`. Gateway logs output:
`"Circuit breaker is OPEN. Activating fallback policy."`
**Triage:**
1. Check database connectivity:
   ```bash
   docker exec -it voxa-postgres pg_isready -U voxa_user -d voxa_db
   ```
2. Verify Redis operational memory:
   ```bash
   redis-cli INFO memory
   ```
**Mitigation:**
1. If PostgreSQL connection pool is exhausted, recycle connections or increase `POOL_SIZE` in `app/config.py`.
2. To manually trigger a Canary reset to `HALF-OPEN`:
   ```bash
   redis-cli SET circuit_breaker:state "HALF-OPEN"
   ```

---

### Playbook 3: P99 Voice Latency Degradation (> 300ms)
**Symptoms:** Prometheus histogram `voxa_request_duration_seconds` shifts to higher buckets.
**Triage:**
1. Check cache hit ratio:
   ```promql
   rate(voxa_semantic_cache_hits_total[5m]) / rate(http_requests_total[5m])
   ```
2. Verify if CPU throttling is active on Faster-Whisper workers.
**Mitigation:**
1. Ensure Whisper compute type is set to `int8` (CTranslate2 AVX-512 vectorization enabled).
2. Warm the Redis semantic vector cache with frequently asked policy embeddings.

---

## 3. Disaster Recovery & Zero-Downtime Deployment
- **Database Backup:** Daily automated pg_dump with WAL archiving.
- **Canary Rollout (Kubernetes):**
  Apply updated container images via progressive rollout:
  ```bash
  kubectl apply -f k8s/gateway-deployment.yaml
  kubectl rollout status deployment/voxa-gateway
  ```
  HPA maintains minimum 2 replicas with scale-up triggers at 70% CPU or 80% memory saturation.
