---
id: RB-04
title: Двойное списание по карте (double charge)
severity: SEV1
services: [payment-service, webhook-processor, card-issuing, postgresql, nginx]
lang: ru
tags: [payment, double-charge, idempotency, webhook, refund]
---

# Двойное списание по карте (double charge)

## Симптом

Пользователь или L1-поддержка сообщают: «Деньги списались дважды». В Grafana срабатывает алерт `payment_duplicate_transactions_total > 0`. В Loki появляются строки вида:

```
level=warning job=webhook-processor msg="webhook retry delivered" txn_id=tx_8f3ac1 attempt=2 user_id=10042
```

## Область и влияние

Затрагивает пользователей, у которых провайдер платёжного шлюза выполнил повторную доставку вебхука (ретрай при таймауте). Деньги реально списаны дважды. Масштаб: от единичного инцидента (SEV3) до массового (SEV1), если ретрай-шторм затронул батч транзакций. Финансовые потери у пользователей реальные.

## Диагностика

**1. Найти дубли в PostgreSQL:**

```sql
-- created_at НЕ группируем: ретрай приходит позже оригинала, иначе дубли разойдутся по разным группам.
SELECT txn_id, user_id, amount,
       COUNT(*) AS cnt,
       MIN(created_at) AS first_seen,
       MAX(created_at) AS last_seen,
       ARRAY_AGG(id ORDER BY created_at) AS row_ids
FROM transactions
WHERE created_at > NOW() - INTERVAL '2 hours'
GROUP BY txn_id, user_id, amount
HAVING COUNT(*) > 1;
-- Пример результата: txn_id=tx_8f3ac1, user_id=10042, amount=1500.00, cnt=2
```

**2. Проверить логи webhook-processor:**

```bash
docker ps | grep webhook-processor
docker logs webhook-processor --since 2h 2>&1 | grep -E "retry|duplicate|tx_8f3ac1"
```

В Loki (LogQL):

```logql
{job="webhook-processor"} |= "retry" | json | txn_id = "tx_8f3ac1"
```

**3. Проверить метрики в Grafana:**

Панель: `Payments / Webhook Processing` → график `payment_webhook_retry_total` и `payment_duplicate_transactions_total`. Резкий рост ретраев — признак отсутствия idempotency-ключа на стороне обработчика.

**4. Убедиться, что оба вебхука прошли:**

```bash
docker logs nginx --since 2h 2>&1 | grep "POST /webhooks/payment" | grep tx_8f3ac1
```

## Решение

**Немедленные действия (временный workaround):**

1. Приостановить обработку входящих вебхуков от затронутого провайдера:
   ```bash
   docker exec nginx nginx -s reload  # после временного disable location в конфиге
   ```
2. Найти все задублированные транзакции (SQL выше) и зафиксировать список.
3. Для каждого дубля выполнить возврат через внутренний API (синтетический пример). Idempotency-Key обязателен: при повторном прогоне ранбука или пересечении списков он защищает от двойного возврата:
   ```bash
   curl -X POST http://localhost:8080/api/v1/refund \
     -H "Content-Type: application/json" \
     -H "Idempotency-Key: refund-tx_8f3ac1-rb04" \
     -d '{"txn_id":"tx_8f3ac1","user_id":10042,"reason":"double_charge_rb04"}'
   ```
4. Уведомить пользователей через Telegram Mini App (шаблон «Ошибочное списание исправлено»).

**Постоянный фикс (разработка):**

Добавить в `webhook-processor` проверку idempotency-ключа перед записью транзакции (колонка `idempotency_key` должна иметь UNIQUE-индекс, иначе при конкурентных ретраях остаётся гонка):

```php
// Laravel: проверка по idempotency_key в таблице webhook_events
WebhookEvent::firstOrCreate(
    ['idempotency_key' => $request->header('Idempotency-Key')],
    ['payload' => $request->all(), 'processed_at' => now()]
);
```

Создать задачу в Яндекс Трекере: `[RB-04] Добавить idempotency-ключ в webhook-processor`.

## Эскалация

- **L1:** Принять обращение, зафиксировать user_id=10042 и txn_id=tx_8f3ac1, открыть тикет в Яндекс Трекере (severity по масштабу: единичный случай — SEV3, признаки массового списания — SEV1), передать в L2 с суммой и временем инцидента.
- **L2:** Выполнить диагностику (SQL, логи), подтвердить масштаб, инициировать возврат через API, держать пользователя в курсе. Если затронуто >10 пользователей — немедленно эскалировать в L3.
- **L3:** Дежурный DevOps/разработчик подключается при массовом сбое или невозможности выполнить возврат. Прикладывает: выгрузку дублей из PostgreSQL, дамп логов Loki за период, скриншот панели Grafana `payment_duplicate_transactions_total`.

## Связано

- [RB-01 — Платёжный вебхук: потеря/дублирование (идемпотентность)](rb-01-webhook-dedup.md)
- [RB-07 — Расхождение сверки с банковским реестром](rb-07-reconciliation-mismatch.md)
