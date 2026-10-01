---
id: RB-09
title: vLLM OOM — контейнер упал из-за нехватки VRAM
severity: SEV2
services: [ai-assistant, vllm, fastapi-facade, rag-index]
lang: ru
tags: [vllm, oom, gpu, infra, ai-assistant]
---

# vLLM OOM: контейнер упал из-за нехватки VRAM на RTX 3070 Ti

## Симптом

Внутренний AI-ассистент перестаёт отвечать. FastAPI-фасад `/ask` возвращает `502 Bad Gateway` или `ConnectionRefusedError`. В Grafana алерт `vLLM /health non-200 > 60s`. Операторы L1 не могут получить подсказку по runbook-у через ассистента.

## Область и влияние

Затронут только внутренний инструмент поддержки — AI-ассистент по операционным runbook-ам. Клиентские транзакции, пополнения СБП и виртуальные карты **не затронуты**. Деньги под угрозой не стоят. Влияние: снижение скорости реакции L1/L2 на инциденты, ручной поиск по runbook-ам.

## Диагностика

**1. Проверить состояние контейнеров:**

```bash
docker ps -a --filter name=vllm
# Ожидаем статус Exited (137) или Exited (1)
```

**2. Посмотреть логи vLLM — найти OOM:**

```bash
docker logs vllm --tail=100 2>&1 | grep -iE "killed|oom|memory|cuda"
```

Пример строки лога, подтверждающей OOM:

```
torch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.80 GiB (GPU 0; 8.00 GiB total capacity; 7.43 GiB already allocated)
```

**3. Метрика GPU в Grafana:**

Открыть дашборд **"vLLM Observability"**, панель `VRAM Used (MiB)` (источник: `nvidia_smi_memory_used_bytes` из gpu-exporter `:9835`). Убедиться, что перед падением значение упиралось в ~8000 MiB.

```promql
nvidia_smi_memory_used_bytes{gpu="0"} / 1024 / 1024
```

**4. Проверить параметры запуска контейнера:**

```bash
docker inspect vllm | grep -A5 "Cmd"
# Искать --max-model-len и --gpu-memory-utilization
```

**5. LogQL в Grafana/Loki — ошибки фасада:**

```logql
{job="facade"} |= "error" | json | line_format "{{.ts}} {{.msg}}"
```

## Решение

**Шаг 1 (временный workaround).** Перезапустить контейнер с уменьшенными параметрами:

```bash
docker stop vllm && docker rm vllm

docker run -d --name vllm --gpus all --ipc=host \
  -p 8000:8000 \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.80 \
  --dtype auto
```

> **Временный workaround:** снижение `--max-model-len` до 4096 ограничивает контекст ассистента. Достаточно для runbook-запросов, но длинные диалоги будут обрезаны.

**Шаг 2.** Убедиться, что фасад и RAG снова здоровы:

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8080/health
```

**Шаг 3 (постоянный фикс).** Зафиксировать параметры в `docker-compose.yml` и закоммитить в GitLab. Провести MR-ревью перед мержем в main.

**Шаг 4.** Убедиться по Grafana, что VRAM держится ниже 7000 MiB в устойчивом состоянии.

## Эскалация

- **L1:** Зафиксировать время начала недоступности ассистента. Попробовать `docker ps -a` и передать вывод L2. Не перезапускать самостоятельно. Передавать при первом подтверждении OOM в логах.
- **L2:** Выполнить диагностику (шаги 1–4), применить временный workaround. Создать тикет в Яндекс Трекере с меткой `ai-infra`. Приложить: вывод `docker logs`, скриншот Grafana (VRAM), текущие параметры `docker inspect`.
- **L3 (дежурный DevOps):** Вызывать, если workaround не помог или VRAM-утечка продолжается после рестарта. Приложить полный `docker inspect`, логи gpu-exporter и снимок метрик за 30 минут до падения.

## Связано

- [RB-10 — Высокая latency p95 — переполнение KV-cache](rb-10-high-latency-kv-cache.md)
- [RB-11 — FastAPI-фасад 502 — vLLM upstream недоступен](rb-11-fastapi-502-vllm-upstream.md)
- [RB-12 — Prometheus scrape DOWN после рестарта WSL](rb-12-prometheus-scrape-wsl-host-docker-internal.md)
