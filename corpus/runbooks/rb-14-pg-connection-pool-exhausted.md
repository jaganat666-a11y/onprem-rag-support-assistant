---
id: RB-14
title: PostgreSQL — исчерпан пул соединений, блокировки от long-running запроса
severity: SEV1
services: [postgresql, laravel-backend, pgbouncer, telegram-mini-app, web, app-max]
lang: ru
tags: [postgresql, connections, locks, pg_stat_activity, pg_locks, blocking-query]
---

# PostgreSQL: исчерпан пул соединений, блокировки от long-running запроса

## Симптом

Пользователи во всех интерфейсах (Telegram Mini App, веб, приложение MAX) получают ошибку «Сервис временно недоступен» или таймаут при пополнении карты и проведении транзакций. В Grafana срабатывает алерт `pg_connections_near_limit`. Laravel-логи заполнены:

```
SQLSTATE[08006] [7] FATAL: remaining connection slots are reserved for non-replication superuser connections
```

## Область и влияние

Затронуты все пользователи: пополнение по СБП, конвертация USD/EUR, выпуск виртуальных карт. Новые транзакции не создаются — прямой риск потери выручки (SEV1). ПДн в примерах маскированы: `user_id=10042`, `txn_id=tx_8f3ac1`, карта `карта ****1111`.

## Диагностика

**1. Проверить число активных соединений:**

```bash
docker exec -it postgres psql -U app_user -d payworld -c \
  "SELECT count(*), state FROM pg_stat_activity GROUP BY state;"
```

**2. Найти long-running и блокирующие запросы:**

```sql
SELECT pid, now() - pg_stat_activity.query_start AS duration,
       usename, application_name, state, wait_event_type, wait_event,
       left(query, 120) AS query_snippet
FROM pg_stat_activity
WHERE state != 'idle'
  AND query_start < now() - interval '30 seconds'
ORDER BY duration DESC;
```

**3. Найти цепочку блокировок:**

```sql
SELECT blocked.pid AS blocked_pid, blocked_act.query AS blocked_query,
       blocking.pid AS blocking_pid, blocking_act.query AS blocking_query
FROM pg_locks blocked
JOIN pg_stat_activity blocked_act   ON blocked_act.pid = blocked.pid
JOIN pg_locks blocking ON blocking.locktype = blocked.locktype
  AND blocking.database     IS NOT DISTINCT FROM blocked.database
  AND blocking.relation     IS NOT DISTINCT FROM blocked.relation
  AND blocking.page         IS NOT DISTINCT FROM blocked.page
  AND blocking.tuple        IS NOT DISTINCT FROM blocked.tuple
  AND blocking.virtualxid   IS NOT DISTINCT FROM blocked.virtualxid
  AND blocking.transactionid IS NOT DISTINCT FROM blocked.transactionid
  AND blocking.classid      IS NOT DISTINCT FROM blocked.classid
  AND blocking.objid        IS NOT DISTINCT FROM blocked.objid
  AND blocking.objsubid     IS NOT DISTINCT FROM blocked.objsubid
  AND blocking.pid != blocked.pid
JOIN pg_stat_activity blocking_act  ON blocking_act.pid = blocking.pid
WHERE NOT blocked.granted AND blocking.granted;
```

**4. Логи контейнера:**

```bash
docker logs --tail=100 postgres 2>&1 | grep -E "ERROR|FATAL|connection"
```

Пример строки: `2026-06-23 03:41:17 UTC [1] FATAL: connection limit exceeded (max_connections=100)`

**5. Метрика в Grafana:** панель `PostgreSQL / Connections` → график `pg_stat_activity_count` по `state`; алерт `pg_connections_near_limit` (порог 90% от `max_connections`).

**6. LogQL в Loki:**

```
{job="laravel"} |= "remaining connection slots"
```

## Решение

**Временный workaround** (немедленно): завершить блокирующий бэкенд по `pid`, полученному на шаге 3:

```sql
SELECT pg_terminate_backend(<blocking_pid>);
```

**Фикс пула:**

```bash
# Проверить, запущен ли PgBouncer
docker ps | grep pgbouncer

# Перезапустить PgBouncer для сброса зависших клиентов
docker restart pgbouncer
```

Убедиться, что суммарный backend-пул PgBouncer (`max_client_conn` / `default_pool_size` в `pgbouncer.ini`) не превышает `max_connections` PostgreSQL за вычетом зарезервированных суперъюзерских слотов. После нормализации числа соединений проверить алерт в Grafana — должен закрыться в течение 2 минут.

**Постоянный фикс:** разобрать причину long-running запроса (`EXPLAIN ANALYZE`), добавить индекс или оптимизировать ORM-запрос; при необходимости поднять `max_connections` в `postgresql.conf` и пересмотреть `pool_size` в PgBouncer.

## Эскалация

- **L1:** зафиксировать алерт в Яндекс Трекере, сообщить пользователям об известной проблеме, передать дежурному L2 с приложением вывода `pg_stat_activity` и скрином Grafana.
- **L2:** выполнить шаги диагностики 1–5, выявить блокирующий `pid`, выполнить `pg_terminate_backend`, перезапустить PgBouncer. Если соединения не освобождаются в течение 5 минут — эскалировать в L3.
- **L3:** дежурный DevOps подключается по SSH, проверяет `max_connections` и конфиг PgBouncer, при необходимости перезапускает контейнер `postgres` с анализом WAL-логов. Прикладывает: полный дамп `pg_stat_activity`, `pg_locks`, docker logs, ссылку на инцидент в Трекере.

## Связано

- [RB-05 — Выпуск виртуальной карты: таймаут BIN-спонсора](rb-05-card-issuance-timeout.md)
- [RB-08 — Telegram Mini App: устаревший баланс](rb-08-tma-balance-cache-stale.md)
- [RB-15 — Диск заполнен — контейнеры в CrashLoop](rb-15-disk-full-crashloop.md)
