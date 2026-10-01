---
id: RB-08
title: "Telegram Mini App: устаревший баланс (кеш / протухший токен сессии)"
severity: SEV2
services: [telegram-mini-app, balance-api, redis, session-service, postgresql]
lang: ru
tags: [balance, cache, session, tma, redis, stale-data]
---

# Telegram Mini App: пользователь видит устаревший или нулевой баланс

## Симптом

Пользователь открывает Telegram Mini App и видит старый баланс (например, 0.00 USD при фактическом остатке 250.00 USD) либо баланс, расходящийся с отображением в приложении MAX. Пополнение через СБП прошло, деньги списаны, но баланс не обновился.

Пример жалобы (L1-тикет): «Пополнил на 5 000 ₽, в MAX вижу 68.14 USD, в TMA — 0.00 USD».

## Область и влияние

Затрагивает пользователей Telegram Mini App при протухшем кеше Redis или истёкшем JWT-токене сессии. Деньги на счёте сохранены — транзакционный ущерб отсутствует, но пользователь лишён актуальной информации о балансе. Масштаб: от единичного (SEV3) до массового при сбое Redis (SEV2). Приложение MAX и веб-интерфейс работают штатно.

## Диагностика

**1. Проверить статус контейнеров и Redis:**
```bash
docker ps --filter "name=balance" --filter "name=redis" --filter "name=session" --format "table {{.Names}}\t{{.Status}}"
docker logs balance-api --since 15m 2>&1 | grep -i "cache\|token\|expired\|redis"
```
Пример строки лога, указывающей на проблему:
```
[2026-06-23 03:41:17] balance-api.ERROR: Redis GET miss for key balance:user:10042, fallback to DB {"user_id":10042}
```

**2. Проверить TTL кеша в Redis:**
```bash
docker exec -it redis redis-cli TTL balance:user:10042
# Если -2 — ключ не существует; если -1 — ключ без TTL (не истечёт сам)
docker exec -it redis redis-cli GET balance:user:10042
```

**3. Проверить токен сессии через LogQL (Loki / Grafana → Explore):**
```logql
{job="session-service"} |= "10042" |= "token" | json | line_format "{{.level}} {{.msg}} {{.user_id}}"
```
Искать: `token expired`, `invalid signature`, `session not found`.

**4. Сверить баланс в PostgreSQL:**
```sql
SELECT u.id, a.balance_usd, a.updated_at
FROM accounts a
JOIN users u ON u.id = a.user_id
WHERE u.id = 10042;
```

**5. Grafana:** панель **TMA / Balance API** → метрика `balance_cache_hit_ratio` (норма > 0.90). Падение ниже 0.5 — массовый Redis-промах.

## Решение

**Шаг 1. Принудительно сбросить кеш конкретного пользователя (временный workaround):**
```bash
docker exec -it redis redis-cli DEL balance:user:10042
```
После сброса следующий запрос TMA подтянет актуальный баланс из БД.

**Шаг 2. Если токен сессии истёк — уведомить пользователя:** попросить выйти и войти в TMA повторно (Telegram переинициализирует WebApp и получит новый JWT).

**Шаг 3. Если Redis недоступен массово:**
```bash
docker restart redis
docker logs redis --since 2m
```
Убедиться, что balance-api восстановил соединение: `docker logs balance-api --since 2m | grep "Redis connected"`.

**Шаг 4. Если масштаб > 20 пользователей — сбросить весь namespace кеша балансов:**
```bash
docker exec -it redis redis-cli --scan --pattern "balance:user:*" | xargs docker exec -i redis redis-cli DEL
```
*(Временный workaround: повышает нагрузку на PostgreSQL на 2–5 минут.)*

## Эскалация

- **L1:** Собрать `user_id`, скриншот TMA и время инцидента. Попросить пользователя перезайти в TMA. Если не помогло в течение 5 минут — открыть тикет в Яндекс Трекере с меткой `balance/tma` и передать в L2.
- **L2:** Выполнить шаги диагностики 1–4. Сбросить кеш пользователя (Шаг 1). Если проблема у >10 пользователей или Redis недоступен — эскалировать в L3 немедленно.
- **L3 (дежурный DevOps/разработчик):** Вызов при массовом сбое Redis, аномалии `balance_cache_hit_ratio < 0.5` на дашборде или невозможности восстановить кеш. Приложить: вывод `docker ps`, логи `balance-api` и `redis` за период инцидента, результат SQL-запроса из п.4, LogQL-дамп из Loki.

## Связано

- [RB-02 — СБП-пополнение зависло в PENDING](rb-02-sbp-pending-timeout.md)
- [RB-11 — FastAPI-фасад 502 — vLLM upstream недоступен](rb-11-fastapi-502-vllm-upstream.md)
- [RB-14 — PostgreSQL — исчерпан пул соединений](rb-14-pg-connection-pool-exhausted.md)
