---
id: RB-15
title: Диск заполнен на ноде — контейнеры в CrashLoop
severity: SEV1
services: [vLLM, FastAPI-фасад, PostgreSQL, Nginx, Loki, Promtail, node-exporter, gpu-exporter]
lang: ru
tags: [disk, crashloop, docker, logs, infra, storage]
---

# Диск заполнен на ноде — контейнеры в CrashLoop

## Симптом

Алерт Grafana: `node_filesystem_avail_bytes{mountpoint="/"} < 5e8` (порог 500 МБ в байтах). Контейнеры непрерывно перезапускаются, в Яндекс Трекере фиксируются жалобы пользователей на недоступность /ask и задержки в Telegram Mini App. В логах Docker характерная строка:

```
[2026-06-23T03:12:44Z] FATAL: no space left on device — failed to write /var/lib/docker/volumes/loki_data/_data/chunks/...
```

## Область и влияние

Затронута вся нода: vLLM, FastAPI-фасад, Loki, Promtail, PostgreSQL не могут писать WAL и логи. Пользователи не получают ответы от AI-ассистента; транзакции (пополнение карт по СБП, конвертация USD/EUR) могут зависать, если PostgreSQL не записывает WAL. Масштаб: все активные сессии. Деньги под угрозой — SEV1.

## Диагностика

**1. Свободное место на диске:**
```bash
df -h /
du -sh /var/lib/docker/* | sort -rh | head -20
```

**2. Крупнейшие docker-логи:**
```bash
docker ps -q | xargs -I{} docker inspect --format='{{.LogPath}} {{.Name}}' {} | \
  xargs -I{} sh -c 'du -sh {} 2>/dev/null' | sort -rh | head -10
```

**3. Статус контейнеров:**
```bash
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.State}}"
```

**4. Grafana — проверить панели:**
- `Node Exporter Full` → `Disk Space Used` (метрика `node_filesystem_avail_bytes`)
- `Docker Overview` → контейнеры в статусе `restarting`
- `GPU Exporter` → `nvidia_smi_memory_used_bytes` (gpu-exporter :9835)

**5. LogQL в Loki (если Loki ещё доступен):**
```logql
{job="facade"} |= "no space left on device"
```
Ограничить вывод последними записями через параметр `limit` панели Grafana (или `--limit` в logcli), а не инлайн в запросе.

**6. PostgreSQL — проверка WAL-накопления:**
```sql
SELECT pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS wal_size;
SELECT pg_size_pretty(pg_database_size('platipay_prod')) AS db_size;
```

## Решение

**1. Экстренная чистка docker-логов (временный workaround):**
```bash
# Усечь самые тяжёлые лог-файлы (не удалять!)
truncate -s 0 $(docker inspect --format='{{.LogPath}}' vllm_container)
truncate -s 0 $(docker inspect --format='{{.LogPath}}' loki)
```

**2. Удалить неиспользуемые docker-объекты:**
```bash
docker system prune -f
# ОСТОРОЖНО: тома (postgres, loki) не трогаем — prune без --volumes их не затрагивает.
# Удаление томов — только отдельной командой после явного подтверждения L3:
docker volume prune -f  # только после явного подтверждения L3 и резервной копии
```

**3. Настроить ротацию логов (постоянное решение) — обновить `docker-compose.yml`:**
```yaml
logging:
  driver: "json-file"
  options:
    max-size: "50m"
    max-file: "5"
```

**4. Применить через Ansible:**
```bash
ansible-playbook -i inventory/prod playbooks/docker_log_rotation.yml
```

**5. Перезапустить контейнеры в CrashLoop:**
```bash
docker compose up -d --force-recreate
docker ps -a  # убедиться, что все Up
```

**6. Проверить восстановление:**
```bash
curl -s http://localhost:8001/health | jq .
df -h /
```

## Эскалация

- **L1:** Зафиксировать алерт в Яндекс Трекере, проверить `df -h` и статус контейнеров `docker ps -a`, передать L2 с выводом команд и временем начала CrashLoop. Не выполнять prune самостоятельно.
- **L2:** Выполнить шаги диагностики 1–5, усечь логи тяжёлых контейнеров (шаг 1 решения), освободить минимум 2 ГБ. Если PostgreSQL недоступен или диск < 200 МБ — немедленно эскалировать L3.
- **L3 (дежурный DevOps):** Подключиться при недоступности PostgreSQL, потере WAL или невозможности освободить место без удаления томов. Приложить: вывод `df -h`, `docker ps -a`, `du -sh /var/lib/docker/*`, LogQL-дамп, тикет Трекера с временной шкалой.

## Связано

- [RB-14 — PostgreSQL — исчерпан пул соединений](rb-14-pg-connection-pool-exhausted.md)
- [RB-13 — Loki/Promtail — лаг ингестии логов](rb-13-loki-promtail-ingest-lag.md)
- [RB-09 — vLLM OOM — контейнер упал по VRAM](rb-09-vllm-oom-crash.md)
