---
id: RB-03
title: "Автоконвертация RUB→USD: провайдер курса недоступен, платёж завис"
severity: SEV1
services: [conversion-service, payment-saga, fx-provider, postgresql, sbp-gateway]
lang: ru
tags: [payment, saga, compensation, fx, partial-state]
---

# Автоконвертация RUB→USD: провайдер курса недоступен, платёж завис

## Симптом

Пользователь сообщает: рубли списаны по СБП (уведомление из банка пришло), но валютный баланс на карте не пополнился. В Telegram Mini App статус платежа завис в `processing`. Алерт: `ConversionSagaStuck` в Grafana (панель **Payment Sagas / Stuck Transactions**) с порогом >0 транзакций старше 5 минут в статусе `FX_PENDING`.

Пример строки лога:

```
[2026-06-23T03:17:42Z] ERROR conversion-service: FX rate fetch failed txn_id=tx_8f3ac1 user_id=10042 amount_rub=5000.00 provider=fx-provider-main status=503 attempt=3/3
```

## Область и влияние

Затронуты все пользователи, пополняющие карту через СБП с автоконвертацией в USD/EUR в момент инцидента. Деньги фактически списаны, но не зачислены — прямой финансовый риск. Масштаб определяется длиной очереди завязших саг.

## Диагностика

**1. Проверить статус контейнеров и связность с FX-провайдером:**

```bash
docker ps --filter name=conversion-service
docker logs --tail=100 conversion-service 2>&1 | grep -E "FX rate|503|timeout"
curl -v --max-time 5 https://fx-provider-main.internal/api/rate?pair=USDRUB
```

**2. Найти завязшие транзакции в PostgreSQL:**

```sql
SELECT txn_id, user_id, amount_rub, status, created_at, updated_at
FROM payment_transactions
WHERE status = 'FX_PENDING'
  AND updated_at < NOW() - INTERVAL '5 minutes'
ORDER BY created_at;
-- Пример ожидаемой строки: txn_id=tx_8f3ac1, user_id=10042, amount_rub=5000.00
```

**3. Проверить логи саги в Loki (LogQL):**

```logql
{job="conversion-service"} |= "FX_PENDING" | json | line_format "{{.txn_id}} {{.status}} {{.msg}}"
{job="sbp-gateway"} |= "tx_8f3ac1"
```

**4. Метрики Grafana:** панель **FX Provider / Response Time** — рост `fx_request_duration_seconds` >3 с или провал `fx_requests_total{status="200"}`. Панель **Payment Sagas / Stuck Transactions** — текущее число завязших саг (gauge `saga_stuck`).

## Решение

**Шаг 1 (временный workaround).** Переключить conversion-service на резервного FX-провайдера. Переменную окружения нужно задать устойчиво (через `-e` / override в compose-конфигурации развёртывания), иначе после рестарта контейнер вернётся к исходному провайдеру; `config:clear` лишь сбрасывает кэш конфига:

```bash
# выставить APP_FX_PROVIDER=fx-provider-fallback в окружении сервиса
# (через docker-compose override / переменную деплоя), затем:
docker exec conversion-service php artisan config:clear
docker restart conversion-service
```

Убедиться, что `curl` к резервному провайдеру возвращает HTTP 200.

**Шаг 2.** Запустить компенсирующую транзакцию для завязших саг (скрипт написан командой, хранится в репозитории):

```bash
php artisan saga:compensate --status=FX_PENDING --older-than=5
```

Скрипт для каждой записи: если валюта не зачислена — возврат рублей на источник СБП и перевод статуса в `REFUNDED`; повторная попытка конвертации ставится в очередь после восстановления провайдера. Операция компенсации **идемпотентна** (защита по `txn_id` / ключу идемпотентности): повторный запуск на тех же `FX_PENDING` не приводит к двойному возврату.

**Шаг 3.** После восстановления основного провайдера — вернуть `APP_FX_PROVIDER=fx-provider-main` в окружении сервиса, перезапустить контейнер, убедиться, что очередь FX_PENDING пуста.

**Шаг 4.** Уведомить затронутых пользователей через сервис нотификаций.

## Эскалация

- **L1:** зафиксировать инцидент в Яндекс Трекере, уведомить пользователей-репортеров о том, что инцидент известен и команда работает. Передать L2 немедленно — деньги списаны.
- **L2:** выполнить диагностику (шаги 1–4), запустить резервного провайдера и компенсирующий скрипт. При отсутствии резервного провайдера или ошибках компенсации — эскалировать на L3 с приложением: вывод `docker logs`, результат SQL-запроса по FX_PENDING, LogQL-дамп, скриншот панели Grafana.
- **L3 (дежурный DevOps/разработчик):** ручная компенсация транзакций, патч саги, при необходимости — откат миграции или экстренный деплой через GitLab CI. Фиксация постмортема.

## Связано

- [RB-02 — СБП-пополнение зависло в PENDING](rb-02-sbp-pending-timeout.md)
- [RB-07 — Расхождение сверки с банковским реестром](rb-07-reconciliation-mismatch.md)
- [RB-05 — Выпуск виртуальной карты: таймаут BIN-спонсора](rb-05-card-issuance-timeout.md)
