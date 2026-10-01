---
id: RB-05
title: Выпуск виртуальной карты: таймаут BIN-спонсора, очередь задач застряла
severity: SEV1
services: [card-issuer, queue-worker, bin-sponsor-api, postgresql, redis]
lang: ru
tags: [payment, card-issuance, timeout, queue, bin-sponsor]
---

# Выпуск виртуальной карты: таймаут BIN-спонсора, очередь задач застряла

## Симптом

Пользователь оплатил тариф (user_id=10042, txn_id=tx_8f3ac1, сумма 499 ₽), но виртуальная карта не появляется в интерфейсе спустя 5+ минут. Алерт Grafana: `card_issue_queue_lag_seconds > 300` и `bin_sponsor_http_errors_total` растёт. В Loki — повторяющиеся строки вида:

```
[2026-06-23T03:17:42Z] ERROR card-issuer: BIN sponsor timeout after 30s | txn=tx_8f3ac1 user=10042 attempt=3
```

## Область и влияние

Затронуты все пользователи, инициировавшие выпуск карты за последние ~30 минут. Деньги списаны (СБП-транзакция прошла), карта не создана — прямое несоответствие баланса. SEV1, требует немедленного реагирования. Каналы: Telegram Mini App, веб, MAX.

## Диагностика

**1. Проверить живость контейнеров:**
```bash
docker ps --filter "name=card-issuer" --filter "name=queue-worker"
```

**2. Посмотреть хвост логов воркера:**
```bash
docker logs --tail=100 -f queue-worker 2>&1 | grep -E "timeout|BIN|error"
```

**3. LogQL в Grafana → Loki (datasource: Loki):**
```logql
{job="card-issuer"} |= "BIN sponsor" | json | level="error" | line_format "{{.ts}} {{.msg}}"
```

**4. Проверить длину очереди в PostgreSQL:**
```sql
SELECT status, COUNT(*) AS cnt, MIN(created_at) AS oldest
FROM card_issue_jobs
WHERE status IN ('pending', 'failed')
GROUP BY status;
```
Признак застрявшей очереди: `pending` > 50 и `oldest` старше 10 минут.

**5. Grafana-панель:** `Card Issuance` → дашборд `Payments Overview` → метрики `card_issue_queue_lag_seconds` и `bin_sponsor_http_errors_total{endpoint="/v1/card/issue"}`.

**6. Доступность BIN-спонсора:**
```bash
curl -v --max-time 10 https://api.bin-sponsor.example.com/v1/health
```

## Решение

**Шаг 1. Остановить повторные попытки, чтобы не наращивать долг очереди (временный workaround):**
```bash
docker exec queue-worker php artisan queue:pause database:card-issuance
```

**Шаг 2.** Если BIN-спонсор недоступен — открыть инцидент на стороне провайдера, ждать восстановления. Параллельно уведомить L2.

**Шаг 3.** После восстановления BIN-спонсора — возобновить очередь и запустить ретрай застрявших задач:
```bash
docker exec queue-worker php artisan queue:resume database:card-issuance
docker exec queue-worker php artisan queue:retry all --queue=card-issuance
```

**Шаг 4.** Проверить, что задачи ушли в `done`, повторным SQL из диагностики.

**Шаг 5. (временный workaround)** Если очередь не рассасывается — вручную перевести `pending`-записи в статус `manual_review` и выпустить карты через admin-панель для пострадавших user_id.

## Эскалация

- **L1:** Убедиться, что деньги списаны, но карта отсутствует. Зафиксировать `user_id`, `txn_id`, время обращения. Не успокаивать пользователя обещаниями без подтверждения от L2. Передать L2 при первом подтверждённом случае немедленно.
- **L2:** Выполнить диагностику (шаги 1–6). Попытаться возобновить очередь после восстановления BIN-спонсора. Открыть тикет в Яндекс Трекере с меткой `SEV1 / card-issuance`. Передать L3, если BIN-спонсор недоступен более 15 минут или ретрай не помогает.
- **L3:** Дежурный DevOps/разработчик. Получить от L2: дамп логов, SQL-отчёт по застрявшим задачам, curl-ответ от BIN-спонсора. Принять решение о ручном выпуске карт, rollback или переключении на резервного эмитента. Обновить статус-страницу и уведомить команду.

## Связано

- [RB-02 — СБП-пополнение зависло в PENDING](rb-02-sbp-pending-timeout.md)
- [RB-14 — PostgreSQL — исчерпан пул соединений](rb-14-pg-connection-pool-exhausted.md)
- [RB-03 — Автоконвертация RUB→USD не прошла](rb-03-rub-usd-conversion-failure.md)
