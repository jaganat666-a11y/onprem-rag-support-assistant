---
id: RB-13
title: Loki Not Ingesting Logs / Promtail Lag
severity: SEV2
services: [loki, promtail, grafana, vllm-facade, node-exporter]
lang: en
tags: [logging, loki, promtail, observability, disk-pressure, positions-file]
---

# Loki Not Ingesting Logs / Promtail Lag

## Симптом

Grafana Explore shows no recent log entries for one or more services (e.g., `{job="facade"}` returns nothing past a certain timestamp). Alert fires: **LokiIngestionLag > 5m** or on-call engineer notices missing logs while investigating another incident. Promtail container may be running but positions file is frozen.

## Область и влияние

Affects all L1–L3 operators relying on Loki/Grafana for real-time log visibility. No direct financial transaction impact, but **incident response is severely degraded**: correlated log evidence for payment failures (e.g., txn_id=tx_8f3ac1, user_id=10042) becomes unavailable. If lag exceeds 30 minutes, SLA on incident resolution times is at risk. The AI runbook assistant (`/ask` facade) also loses live log context fed via RAG pipeline.

## Диагностика

**1. Confirm Promtail is running and check its own logs:**
```bash
docker ps --filter name=promtail
docker logs promtail --tail=100 2>&1 | grep -E "error|warn|lag|positions"
```
Expected error line:
```
level=error msg="error reading positions file" path=/tmp/positions.yaml err="unexpected EOF"
```

**2. Check Loki readiness and disk pressure:**
```bash
curl -s http://localhost:3100/ready
# Expected: "ready"

df -h /var/lib/docker/volumes/loki_data
# Alert if >85% used
```

**3. Inspect Promtail positions file:**
```bash
docker exec promtail cat /tmp/positions.yaml
# Look for stale/identical offsets across multiple scrape cycles
```

**4. Query Loki ingestion lag in Grafana:**
- Dashboard: **Loki / Chunks** → panel `Ingester: Log Entries Received Rate`
- LogQL health check:
```logql
{job="facade"} |= "error" | json | line_format "{{.ts}} {{.msg}}"
```
- Metric: `promtail_read_bytes_total` flat line while `promtail_file_bytes_total` keeps growing = read offset (positions) stuck. (`promtail_file_bytes_total` tracks file size, `promtail_read_bytes_total` tracks how far Promtail has read; a growing gap means Promtail is falling behind.)

**5. Check Loki WAL and chunk flusher:**
```bash
docker logs loki --tail=200 2>&1 | grep -E "chunk|flush|pressure|OOO"
```

**6. Correlate with incident log in PostgreSQL:**
```sql
SELECT created_at, service, message
FROM incident_log
WHERE created_at > NOW() - INTERVAL '1 hour'
  AND service = 'loki'
ORDER BY created_at DESC
LIMIT 20;
```

## Решение

**Fix A — Corrupted positions file (most common):**
```bash
docker stop promtail
docker exec promtail rm /tmp/positions.yaml   # positions file lives in the promtail container; or path from config
docker start promtail
docker logs -f promtail                        # verify re-scrape begins
```
> **Temporary workaround:** restart alone (`docker restart promtail`) clears in-memory lag but does not fix a corrupt file — use only as a first-pass stabiliser while diagnosing.

**Fix B — Loki disk pressure:**
```bash
# Primary fix: expand the volume or tighten retention, then redeploy
# (set limits_config.retention_period + compactor.retention_enabled=true in loki config)
docker compose up -d loki

# Targeted deletion (only if compactor retention is enabled): use POST, not a disk-freeing shortcut.
# Deletion is processed after delete_request_cancel_period (default 24h), so it does NOT free space immediately.
curl -s -X POST \
  'http://localhost:3100/loki/api/v1/delete?query={job="test"}&start=1970-01-01T00:00:00Z&end=2020-01-01T00:00:00Z'
```

**Fix C — Loki OOM / crash loop:**
```bash
docker stats loki
# If restarting: increase mem_limit in compose, redeploy
```

After any fix, confirm recovery (log queries must use the range endpoint, not the instant `/query` endpoint):
```bash
curl -s -G 'http://localhost:3100/loki/api/v1/query_range' \
  --data-urlencode 'query={job="facade"}' \
  --data-urlencode "start=$(date -d '5 minutes ago' +%s)000000000" \
  --data-urlencode 'limit=5' | jq '.data.result[].values[0]'
```

## Эскалация

- **L1:** Confirm symptom in Grafana Explore (`{job="facade"}` blank). Restart Promtail (`docker restart promtail`). If logs resume within 5 minutes — resolved; log in Yandex Tracker. Otherwise escalate to L2 with: Grafana screenshot, `docker logs promtail` last 200 lines, `df -h` output.
- **L2:** Run full diagnostics (steps 1–6 above). Apply Fix A or B. Verify ingestion rate recovers in Grafana. If Loki itself is crash-looping or disk cleanup is insufficient — escalate to L3. Attach: positions file content, Loki logs, disk usage report, incident_log SQL output.
- **L3 (on-call DevOps):** Engage if disk expansion required (Yandex Cloud volume resize via Ansible), or if Loki data corruption suspected (chunk index rebuild). Attach full docker inspect outputs for loki and promtail containers, compose file, and Prometheus snapshot (`curl -XPOST http://localhost:9090/api/v1/admin/tsdb/snapshot`, requires Prometheus started with `--web.enable-admin-api`).

## Связано

- [RB-12 — Prometheus scrape DOWN after WSL restart](rb-12-prometheus-scrape-wsl-host-docker-internal.md)
- [RB-11 — FastAPI facade 502 — vLLM upstream down](rb-11-fastapi-502-vllm-upstream.md)
- [RB-15 — Disk full — containers in CrashLoop](rb-15-disk-full-crashloop.md)
