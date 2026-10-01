---
id: RB-01
title: Платёжный вебхук — потеря или дублирование события от эквайера
severity: SEV1
services: [webhook-handler, payment-service, postgresql, nginx, sbp-gateway]
lang: ru
tags: [payment, webhook, idempotency, dedup, sbp, acquirer]
---

# Платёжный вебхук: потеря или дублирование события от эквайера

## Симптом

Пользователь (user_id=10042) сообщает: пополнение прошло по СБП (txn_id=tx_8f3ac1, сумма 5 000 ₽), списание в банке подтверждено, но баланс карты пользователя не изменился. Либо обратная ситуация: баланс вырос дважды. Алерт в Grafana: `payment_webhook_missing_total` > 0 или `payment_webhook_duplicates_total` > 0.

## Область и влияние

Затрагивает всех пользователей, ожидающих зачисления в текущем временном окне. Деньги клиента списаны — финансовый риск. При двойном зачислении — риск убытка компании. Все интерфейсы (Telegram Mini App, веб, MAX) показывают некорректный баланс.

## Диагностика

**1. Проверить, дошёл ли вебхук до Nginx:**
```bash
docker logs nginx --since 10m 2>&1 | grep "POST /api/webhooks/payment"
# Ожидаемая строка: 10.0.0.5 - - [23/Jun/2026:14:31:02 +0000] "POST /api/webhooks/payment HTTP/1.1" 200 42
```

**2. Логи контейнера webhook-handler (Loki / LogQL):**
```logql
{job="webhook-handler"} |= "tx_8f3ac1"
```
Пример подозрительной строки:
```
[2026-06-23 14:31:05] ERROR: Duplicate event_id=evt_cc2901 for txn tx_8f3ac1 — skipped
```

**3. Проверить идемпотентность в PostgreSQL:**
```sql
SELECT event_id, txn_id, status, created_at, processed_at
FROM webhook_events
WHERE txn_id = 'tx_8f3ac1'
ORDER BY created_at;
```
Два ряда с одним `event_id` = дубль; отсутствие ряда = вебхук не дошёл.

**4. Метрика в Grafana:** панель **Payments / Webhook Health** → графики `payment_webhook_received_total`, `payment_webhook_duplicates_total`, `payment_webhook_missing_total`. Провал или всплеск — ориентир по времени инцидента.

**5. Доступность эндпоинта:**
```bash
curl -v -X POST https://pay.example.internal/api/webhooks/payment \
  -H "X-Signature: test" -d '{"event_id":"probe-1","txn_id":"probe"}' 2>&1 | head -20
```

## Решение

1. **Потеря вебхука.** Убедиться, что Nginx принял запрос (шаг 1). Если нет — проверить сертификат и firewall на стороне эквайера. Запросить повторную отправку события у эквайера через его портал или API. Вручную запустить команду зачисления (временный workaround — только L2/L3):
```bash
docker exec payment-service php artisan payment:replay --txn tx_8f3ac1
```
> **ВРЕМЕННЫЙ WORKAROUND** — использовать только при подтверждённой потере, после проверки отсутствия записи в `webhook_events`.

2. **Дублирование.** Убедиться, что в `webhook_events` проставлен `UNIQUE(event_id)` и обработчик делает `INSERT ... ON CONFLICT DO NOTHING` до обновления баланса. Если ограничение отсутствует — применить миграцию:
```sql
ALTER TABLE webhook_events ADD CONSTRAINT uq_event_id UNIQUE (event_id);
```
Откатить двойное зачисление вручную только через L3 с согласованием финансового контроля.

3. В обоих случаях создать тикет в Яндекс Трекере с типом «Инцидент», приложить event_id, txn_id, дамп из `webhook_events`.

## Эскалация

- **L1:** Зафиксировать user_id=10042, txn_id=tx_8f3ac1, время обращения. Проверить статус в ЛК. Открыть тикет. Если подтверждена потеря/дубль — передать L2 немедленно (< 10 мин).
- **L2:** Выполнить диагностику (шаги 1–5). Оценить масштаб: сколько транзакций затронуто за последние 30 мин (`SELECT COUNT(*) FROM webhook_events WHERE status='pending' AND created_at > NOW()-INTERVAL '30 min'`). При >5 затронутых пользователях или двойных зачислениях — немедленно эскалировать в L3.
- **L3:** Дежурный DevOps + разработчик платёжного модуля. Приложить: дамп `webhook_events`, вывод LogQL, скриншот панели Grafana, вывод `curl` к эндпоинту, список event_id. При двойном зачислении — согласовать откат с финансовым контролем до применения.

## Связано

- [RB-04 — Двойное списание по карте](rb-04-double-charge.md)
- [RB-06 — Webhook signature mismatch после ротации секрета](rb-06-webhook-signature-mismatch.md)
- [RB-02 — СБП-пополнение зависло в PENDING](rb-02-sbp-pending-timeout.md)
