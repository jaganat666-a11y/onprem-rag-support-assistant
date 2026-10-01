---
id: RB-07
title: Расхождение сверки — сумма в БД не совпадает с банковским реестром
severity: SEV2
services: [reconciliation, payments, postgresql, sbp-gateway, currency-conversion]
lang: ru
tags: [reconciliation, mismatch, sbp, currency, rounding, fintech]
---

# Расхождение сверки: сумма в БД не совпадает с банковским реестром

## Симптом

В алерт Grafana прилетает `reconciliation_mismatch_count > 0` по итогам дневного прогона. Финансовый аналитик или L1-дежурный обнаруживает, что суммарный оборот по транзакциям в нашей PostgreSQL отличается от суммы в банковском реестре (CSV/XML-файл, поступает в 23:30 UTC). Разрыв может быть как в единицы рублей (проблема округления), так и в несколько тысяч (ошибка курса на момент T).

Пример строки лога фасада:

```
2026-06-23T23:47:12Z [ERROR] recon job=daily_recon date=2026-06-23 db_total=1482310.52 bank_total=1482287.00 delta=23.52 mismatched_txns=4
```

## Область и влияние

Затрагивает всех пользователей, чьи транзакции попали в расходящиеся строки реестра. Деньги клиентов не теряются, но финансовая отчётность недостоверна. При дельте > 1 000 ₽ или > 10 расходящихся транзакций требуется ручная сверка до открытия следующего операционного дня.

## Диагностика

**1. Проверить лог сервиса сверки:**

```bash
docker logs --since=24h recon-service 2>&1 | grep -E "mismatch|delta|ERROR"
```

**2. LogQL в Grafana / Loki:**

```logql
{job="recon-service"} |= "mismatch" | logfmt | delta > 0
```

**3. Grafana-панель:** `Financial / Reconciliation` → дашборд `Recon Daily`, график `recon_mismatch_delta_rub` и таблица `mismatched_txn_ids`.

**4. Найти расходящиеся транзакции в PostgreSQL:**

```sql
-- Транзакции, сумма которых в БД не совпадает с суммой из реестра банка
SELECT
    t.id                         AS txn_id,
    t.user_id,
    t.amount_rub,
    t.amount_usd,
    t.fx_rate,
    t.created_at,
    b.bank_amount_rub,
    (t.amount_rub - b.bank_amount_rub) AS delta_rub
FROM transactions t
JOIN bank_recon_import b ON b.bank_txn_ref = t.external_ref
WHERE DATE(t.created_at) = CURRENT_DATE - 1
  AND ABS(t.amount_rub - b.bank_amount_rub) > 0.01
ORDER BY ABS(delta_rub) DESC
LIMIT 50;
```

**5. Проверить таблицу курсов на момент транзакции:**

```sql
SELECT fx_rate, valid_from, valid_to
FROM fx_rates
WHERE currency_pair = 'RUB_USD'
  AND valid_from <= '2026-06-23 14:33:00'
ORDER BY valid_from DESC
LIMIT 5;
```

Типичная причина: курс фиксируется на `created_at`, а банк считает по курсу `settlement_at` (+5–30 мин). Ещё одна причина: PHP `round($amount, 2)` против банковского `TRUNCATE(amount, 2)`.

## Решение

1. Выгрузить список `txn_id` с дельтой из SQL выше.
2. Для каждой транзакции сверить поле `fx_rate` с архивом курсов (`fx_rates`). Если курс расходится — пересчитать сумму вручную и сформировать корректирующую запись в `reconciliation_adjustments`.
3. **(Временный workaround)** Если дельта < 50 ₽ и причина подтверждена как округление: создать задачу в Яндекс Трекер с меткой `recon-rounding`, провести корректировку через `reconciliation_adjustments` до следующего прогона.
4. Постоянный фикс: привести стратегию округления в коде к `bcmath` с `scale=2` и согласовать с банком единый момент фиксации курса (`created_at` vs `settlement_at`).
5. После правки запустить повторный прогон сверки:

```bash
docker exec recon-service php artisan recon:run --date=2026-06-23 --force
```

## Эскалация

- **L1:** Проверить алерт в Grafana, зафиксировать дельту и количество транзакций. Если дельта ≤ 100 ₽ — завести тикет в Яндекс Трекер, передать в L2. Если дельта > 100 ₽ или > 10 транзакций — немедленно будить L2, не ждать утра.
- **L2:** Запустить SQL-запросы из раздела «Диагностика», установить причину (курс/округление/дублирование). Применить workaround при необходимости. Приложить к тикету: дату реестра, значения `db_total`, `bank_total`, список `txn_id` с дельтами.
- **L3:** Зовём дежурного разработчика, если причина не установлена за 60 мин или дельта > 10 000 ₽. Прикладываем: дамп таблицы `bank_recon_import` за дату, SQL-результаты, скрин панели Grafana, лог контейнера `recon-service`.

## Связано

- [RB-03 — Автоконвертация RUB→USD не прошла](rb-03-rub-usd-conversion-failure.md)
- [RB-04 — Двойное списание по карте](rb-04-double-charge.md)
- [RB-02 — СБП-пополнение зависло в PENDING](rb-02-sbp-pending-timeout.md)
