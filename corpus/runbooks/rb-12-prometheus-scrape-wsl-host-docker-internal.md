---
id: RB-12
title: Prometheus scrape DOWN — host.docker.internal не резолвится после рестарта WSL
severity: SEV2
services: [prometheus, vllm, node-exporter, grafana, ai-assistant]
lang: ru
tags: [infra, prometheus, wsl2, scrape, grafana, vllm, monitoring]
---

# Prometheus scrape DOWN: host.docker.internal не резолвится после рестарта WSL

## Симптом

Grafana-дашборды AI-ассистента показывают **No data** на всех панелях. В Prometheus UI (`http://localhost:9090/targets`) таргеты `vllm` и `node-exporter` в состоянии **DOWN** с ошибкой вида:

```
Get "http://host.docker.internal:8000/metrics": dial tcp: lookup host.docker.internal: no such host
```

Алерт: `PrometheusTargetDown` / `AIAssistantScrapeDown` в Grafana Alerting.

## Область и влияние

Затронут внутренний AI-ассистент (runbook RAG, /ask, /health). Продуктовый трафик (СБП-пополнения, выпуск карт, Telegram Mini App) **не затронут**. Мониторинг AI-инфраструктуры слеп: метрики vLLM (KV-cache usage, latency), node-exporter (CPU/RAM хоста) и gpu-exporter (GPU VRAM) не собираются. Деньги пользователей не под угрозой.

## Диагностика

**1. Проверить статус таргетов в Prometheus:**
```bash
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | {job: .labels.job, health: .health, lastError: .lastError}'
```
Ожидаемый признак проблемы: `"health": "down"`, `"lastError": "no such host"`.

**2. Проверить резолвинг host.docker.internal внутри контейнера Prometheus:**
```bash
docker exec prometheus nslookup host.docker.internal
# или
docker exec prometheus getent hosts host.docker.internal
```
Если команда зависает или возвращает `NXDOMAIN` — проблема подтверждена.

**3. Убедиться, что WSL2 и Docker Desktop перезапущены:**
```bash
wsl --list --running
# ожидаем: Ubuntu Running
docker info | grep -i "server version"
```

**4. Проверить prometheus.yml на корректность job-конфигурации:**
```yaml
# prometheus.yml (фрагмент)
- job_name: vllm
  static_configs:
    - targets: ['host.docker.internal:8000']
- job_name: node
  static_configs:
    - targets: ['host.docker.internal:9100']
```

**5. Логи Prometheus на scrape-ошибки (Loki):**
```logql
{job="prometheus"} |= "no such host"
```
Пример строки лога:
```
level=debug ts=2026-06-23T03:17:42Z caller=scrape.go:1507 component="scrape manager" scrape_pool=vllm target=http://host.docker.internal:8000/metrics msg="Scrape failed" err="Get \"http://host.docker.internal:8000/metrics\": dial tcp: lookup host.docker.internal on 127.0.0.11:53: no such host"
```

**6. Проверить, слушает ли vLLM нужный порт на хосте WSL2:**
```bash
# В WSL2 (Ubuntu)
ss -tlnp | grep 8000
```

## Решение

**Шаг 1 (временный workaround).** Узнать IP-адрес хоста WSL2 и подставить его напрямую:
```bash
# В WSL2
ip route show default | awk '{print $3}'
# Пример вывода: 172.17.112.1
```
Временно заменить `host.docker.internal` на полученный IP в `prometheus.yml` и перезапустить Prometheus:
```bash
docker compose restart prometheus
```
> **Временный фикс.** IP меняется при каждом рестарте WSL2.

**Шаг 2 (постоянное решение).** Добавить `extra_hosts` в `docker-compose.yml` для сервиса Prometheus:
```yaml
services:
  prometheus:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```
Затем пересоздать контейнер:
```bash
docker compose up -d --force-recreate prometheus
```

**Шаг 3.** Убедиться, что таргеты поднялись:
```bash
curl -s http://localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.health=="up") | .labels.job'
```

**Шаг 4.** Проверить Grafana: дашборды `AI Assistant Overview` и `Node Exporter` должны показывать данные.

## Эскалация

- **L1:** Зафиксировать алерт в Яндекс Трекере. Открыть `http://localhost:9090/targets`, сделать скриншот. Проверить, доступен ли `/ask` и `/health` фасада (`curl http://localhost:8080/health`). Если фасад отвечает — продуктового инцидента нет, передать L2 с приложенным скриншотом /targets и описанием когда был рестарт WSL.
- **L2:** Выполнить шаги диагностики 1–6. Применить временный workaround (Шаг 1). Если `extra_hosts: host-gateway` не помог — проверить версию Docker (фича `host-gateway` требует Docker Engine ≥ 20.10 / Docker Desktop ≥ 3.x: `docker version`). При подтверждении фикса обновить `docker-compose.yml` и закрыть тикет с комментарием.
- **L3 (дежурный DevOps):** Привлекать, если: workaround не устранил DOWN за 30 минут; проблема воспроизводится после `force-recreate`; подозрение на деградацию GPU-хоста или сетевого стека WSL2 (приложить `wsl --status`, `docker info`, лог `dmesg | tail -50` из WSL2).

## Связано

- [RB-09 — vLLM OOM — контейнер упал по VRAM](rb-09-vllm-oom-crash.md)
- [RB-13 — Loki/Promtail — лаг ингестии логов](rb-13-loki-promtail-ingest-lag.md)
- [RB-10 — Высокая latency p95 — переполнение KV-cache](rb-10-high-latency-kv-cache.md)
