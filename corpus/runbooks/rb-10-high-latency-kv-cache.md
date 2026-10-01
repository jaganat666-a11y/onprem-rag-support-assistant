---
id: RB-10
title: Высокая latency p95 ассистента: переполнение KV-cache и preemption
severity: SEV2
services: [vllm, fastapi-facade, prometheus, grafana, loki]
lang: ru
tags: [latency, kv-cache, preemption, vllm, inference, performance]
---

# Высокая latency p95 ассистента: переполнение KV-cache и preemption

## Симптом

Алерт `AssistantP95LatencyHigh` в Grafana: p95 e2e latency превысила 8 с (порог). Пользователи наблюдают зависание запросов к `/ask` на 10–30 с. Throughput (req/s) не растёт. В логах фасада появляются записи вида:

```
2026-06-23T03:17:42Z [WARNING] vllm request queued 14.3s, preemption detected, seq_id=8841
```

## Область и влияние

Затронуты все пользователи внутреннего AI-ассистента (L2/L3, дежурные DevOps). Операции с деньгами не затронуты напрямую — сервис справочный. При длительной деградации замедляется реакция дежурных на инциденты основного продукта, что косвенно повышает риск для транзакций.

## Диагностика

**1. Проверить статус контейнеров:**
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "vllm|facade"
```

**2. Логи vLLM — найти preemption и очередь:**
```bash
docker logs vllm --since 15m 2>&1 | grep -E "preempt|cache|queue|OOM"
```
Ожидаемая строка: `Running: 12 reqs, Swapped: 3 reqs, Pending: 8 reqs, GPU KV cache usage: 97.4%`

**3. Логи фасада через Loki (LogQL):**
```
{job="facade"} |= "preemption" | json | line_format "{{.ts}} {{.msg}}"
{job="facade"} |= "error" | json | duration > 8s
```

**4. Метрики в Grafana** — панель `AI Assistant / Inference`:
- `histogram_quantile(0.95, sum by (le) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))` — рост p95 выше 8 с.
- `vllm:gpu_cache_usage_perc` — приближается к 100 %.
- `vllm:num_preemptions_total` — ненулевой и растущий счётчик.
- `vllm:num_requests_waiting` — очередь ожидающих запросов не рассасывается.

**5. GPU и RAM хоста:**
```bash
nvidia-smi --query-gpu=memory.used,memory.free --format=csv
free -h
```

**6. Активные соединения к фасаду:**
```bash
ss -tlnp | grep 8000
docker stats vllm --no-stream
```

**7. Инцидент-журнал PostgreSQL — последние медленные запросы фасада:**
```sql
SELECT created_at, user_id, request_id, latency_ms, status
FROM incident_log
WHERE created_at > now() - interval '30 minutes'
  AND latency_ms > 8000
ORDER BY created_at DESC
LIMIT 20;
-- Пример строки: user_id=<masked>, request_id='req_xxxxxx', latency_ms=14320, status='timeout'
```

## Решение

**Шаг 1. Снизить concurrency (временный workaround).**
Ограничить параллельные запросы к фасаду через Nginx upstream:
```nginx
# /etc/nginx/conf.d/facade.conf — временно
upstream facade {
    server 127.0.0.1:8001;
    keepalive 4;  # было 16
}
```
```bash
nginx -t && nginx -s reload
```

**Шаг 2. Перезапустить vLLM с уменьшенным `max-num-seqs`.**
Параметры concurrency и памяти vLLM читает из CLI-флагов entrypoint (`vllm serve`), а НЕ из переменных окружения — передаём их как аргументы, иначе фикс не применится:
```bash
docker stop vllm
docker run -d --name vllm --gpus all \
  -p 8000:8000 \
  vllm-image:latest \
  --model /models/Qwen2.5-7B-AWQ --quantization awq \
  --max-num-seqs 4 \            # было 16
  --gpu-memory-utilization 0.88
```
После старта убедиться: `curl -s http://localhost:8000/health | jq .`

**Шаг 3. Проверить восстановление метрик** — в Grafana p95 должна опуститься ниже 4 с в течение 3–5 мин.

**Шаг 4 (постфактум).** Настроить `--max-model-len` под реальный рабочий контекст (если запросы содержат длинный RAG-контекст — обрезать чанки до 512 токенов в pipeline bge-m3). Зафиксировать значения в Ansible-роли `roles/vllm/defaults/main.yml`.

## Эскалация

- **L1:** Зафиксировать алерт, снять скриншот панели Grafana `AI Assistant / Inference`, убедиться, что основные сервисы (платежи, СБП) живы. Если деградация AI-ассистента продолжается >10 мин — передать L2, приложив скриншот и вывод `docker logs vllm --since 15m`.
- **L2:** Выполнить шаги диагностики 1–6. Применить временный workaround (шаг 1 решения). Если p95 не снижается за 5 мин после workaround — эскалировать L3, передав: LogQL-выгрузку, вывод `nvidia-smi`, значения метрик `gpu_cache_usage_perc` и `num_preemptions_total`.
- **L3 (дежурный DevOps):** Перезапустить vLLM с новыми параметрами (шаг 2). Провести root-cause анализ: проверить, не выросла ли средняя длина входного контекста (RAG-чанки). Обновить Ansible-роль и зафиксировать в Яндекс Трекере задачу на постоянный фикс.

## Связано

- [RB-09 — vLLM OOM — контейнер упал по VRAM](rb-09-vllm-oom-crash.md)
- [RB-11 — FastAPI-фасад 502 — vLLM upstream недоступен](rb-11-fastapi-502-vllm-upstream.md)
