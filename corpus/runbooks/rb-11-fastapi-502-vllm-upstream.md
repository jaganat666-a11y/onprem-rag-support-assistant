---
id: RB-11
title: FastAPI-фасад 502 — upstream vLLM недоступен
severity: SEV2
services: [ai-assistant-facade, vllm, fastapi, loki, grafana, prometheus]
lang: ru
tags: [infra, ai-assistant, vllm, fastapi, 502, upstream, docker]
---

# FastAPI-фасад 502: upstream vLLM недоступен

## Симптом

Алерт `AiFacadeHealthRed` в Grafana / PagerDuty. Запросы к `/ask` и `/health` возвращают `502 Bad Gateway`. Внутренние инструменты поддержки, использующие AI-ассистента, недоступны. Пример ответа:

```
HTTP/1.1 502 Bad Gateway
{"detail": "Upstream vLLM unreachable: Connection refused to http://vllm:8000"}
```

## Область и влияние

Затронут внутренний AI-ассистент по runbook-ам — инструмент L1/L2-поддержки. Клиентские транзакции, СБП-пополнения, карты Visa/Mastercard не затронуты напрямую. Косвенный эффект: замедление обработки инцидентов операторами поддержки. Деньги клиентов под угрозой не стоят. Дежурным L2 сообщить в течение 15 минут с момента алерта.

## Диагностика

**1. Проверить состояние контейнеров:**

```bash
docker ps --filter name=vllm --filter name=facade
```

Ожидаем статус `Up`. Если `Exited` или контейнер отсутствует — причина найдена.

**2. Логи фасада в Loki (LogQL):**

```logql
{job="facade"} |= "error" | json | line_format "{{.ts}} {{.level}} {{.msg}}"
```

Характерная строка ошибки:

```
2026-06-23T03:14:07Z ERROR httpx._client connect error: [Errno 111] Connection refused ('vllm', 8000)
```

**3. Логи самого vLLM:**

```bash
docker logs vllm --tail=100 --since=30m
```

Искать: `CUDA out of memory`, `Killed`, `RuntimeError`, `model loading`.

**4. Проверить доступность порта vLLM изнутри хоста:**

```bash
curl -v http://localhost:8000/health
ss -tlnp | grep 8000
```

**5. Метрика в Grafana:**

Панель **AI Assistant / Facade Health** → метрика `facade_vllm_upstream_errors_total`. Всплеск на графике укажет момент падения.

**6. Проверить инцидент-журнал в PostgreSQL (наличие связанных инцидентов):**

```sql
SELECT id, created_at, service, message
FROM incident_log
WHERE service = 'ai-assistant-facade'
  AND created_at > NOW() - INTERVAL '1 hour'
ORDER BY created_at DESC
LIMIT 10;
```

## Решение

**Шаг 1.** Если vLLM-контейнер упал — перезапустить:

```bash
docker compose restart vllm
# Подождать ~60–90 с на загрузку модели
curl http://localhost:8000/health
```

**Шаг 2.** Если OOM (GPU): убедиться, что сторонние процессы не занимают VRAM:

```bash
nvidia-smi
docker compose restart vllm
```

**Шаг 3.** Если контейнер жив, но порт не слушает — полный рестарт стека:

```bash
docker compose down && docker compose up -d
```

**Шаг 4 (временный workaround).** Пока vLLM не поднят — направить операторов на статичный HTML-зеркальный индекс runbook-ов (`/runbooks/static`), сообщить L1 через служебный чат.

## Эскалация

- **L1:** Зафиксировать факт алерта, проверить `/health` через браузер, сообщить в L2-чат «Ассистент недоступен, алерт RB-11» — передать немедленно, самостоятельно не перезапускать.
- **L2:** Выполнить диагностику по шагам 1–4, перезапустить контейнер vLLM. Если не помогло за 10 минут — эскалировать в L3. Приложить: вывод `docker ps`, последние 50 строк `docker logs vllm`, ответ `curl /health`.
- **L3 (дежурный DevOps):** Подключиться к WSL2-хосту, проверить состояние GPU (`nvidia-smi`), состояние модели, свободное место на диске (`df -h`). При необходимости — пересобрать образ или откатить версию vLLM через GitLab CI.

## Связано

- [RB-09 — vLLM OOM — контейнер упал по VRAM](rb-09-vllm-oom-crash.md)
- [RB-10 — Высокая latency p95 — переполнение KV-cache](rb-10-high-latency-kv-cache.md)
- [RB-13 — Loki/Promtail — лаг ингестии логов](rb-13-loki-promtail-ingest-lag.md)
