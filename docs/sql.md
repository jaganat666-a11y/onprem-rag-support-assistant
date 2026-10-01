# SQL-диагностика: журнал запросов `ask_log`

Каждый обработанный `/ask` фасад пишет строкой в PostgreSQL (сервис `postgres`,
схема — [db/init.sql](../db/init.sql)). Когда приходят с жалобой «ассистент
тормозит» или «отвечает ошибками», ответ достаётся обычным SQL — ниже рабочий
набор запросов.

Подключение к базе (изнутри контейнера, клиент `psql` уже там):

```bash
docker compose exec postgres psql -U facade -d asklog
```

Полезное в `psql`: `\dt` — список таблиц, `\d ask_log` — схема таблицы,
`\x` — вертикальный вывод широких строк, `\q` — выход.

## 1. Что происходит прямо сейчас: последние 20 запросов

```sql
SELECT ts, status, latency_ms, left(question, 60) AS question, error
FROM ask_log
ORDER BY ts DESC
LIMIT 20;
```

## 2. Сколько ошибок за последний час (первый вопрос при алерте)

```sql
SELECT count(*) FILTER (WHERE status >= 500) AS errors,
       count(*)                              AS total
FROM ask_log
WHERE ts > now() - interval '1 hour';
```

## 3. Динамика по часам за сутки: когда началось?

```sql
SELECT date_trunc('hour', ts)                    AS hour,
       count(*)                                  AS total,
       count(*) FILTER (WHERE status >= 500)     AS errors,
       round(avg(latency_ms)::numeric, 0)        AS avg_ms
FROM ask_log
WHERE ts > now() - interval '24 hours'
GROUP BY 1
ORDER BY 1;
```

## 4. Хвост латентности: p95 и топ самых медленных

Среднее скрывает проблемы — смотрим 95-й перцентиль (5% самых медленных
запросов дольше него) и сами медленные запросы. `latency_ms` — полное время
ответа на фасаде:

```sql
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM ask_log
WHERE status = 200 AND ts > now() - interval '24 hours';

SELECT ts, latency_ms, completion_tokens, left(question, 60) AS question
FROM ask_log
WHERE status = 200
ORDER BY latency_ms DESC
LIMIT 10;
```

## 5. Какие runbook'и реально работают (частота попадания в контекст)

`sources` — массив; `unnest` разворачивает его в строки, дальше обычный GROUP BY:

```sql
SELECT unnest(sources) AS runbook, count(*) AS hits
FROM ask_log
WHERE status = 200
GROUP BY 1
ORDER BY hits DESC
LIMIT 10;
```

## 6. Выходные токены за сутки

```sql
SELECT count(*)                    AS answers,
       sum(completion_tokens)      AS tokens,
       round(avg(completion_tokens)::numeric, 0) AS avg_tokens
FROM ask_log
WHERE status = 200 AND ts > now() - interval '24 hours';
```

Журнал хранит только выходные токены — в среднем 0,27 тыс. на ответ. Входные
(вопрос и runbook'и, ~1,8 тыс.) он не пишет, поэтому полный расход и цену
ответа отсюда не посчитать; оценка цены — в [pilot.md](pilot.md).

## 7. Оценки пользователей: доля 👎 по дням

`/ask` возвращает `ask_id`, пользователь ставит оценку через
`POST /feedback {"ask_id": 42, "rating": -1, "comment": "..."}` — она ложится в
ту же строку журнала (колонки `rating`, `feedback_comment`, `feedback_ts`).

```sql
SELECT date_trunc('day', ts)                AS day,
       count(*)                             AS answers,
       count(*) FILTER (WHERE rating = 1)   AS up,
       count(*) FILTER (WHERE rating = -1)  AS down
FROM ask_log
WHERE status = 200 AND ts > now() - interval '7 days'
GROUP BY 1
ORDER BY 1;
```

## 8. Плохие ответы: что разбирать

```sql
SELECT id, feedback_ts, left(question, 70) AS question, sources, feedback_comment
FROM ask_log
WHERE rating = -1
ORDER BY feedback_ts DESC
LIMIT 20;
```

`sources` подсказывает направление разбора: нужного runbook'а нет в списке —
промах поиска; есть, а ответ неверный — промах генерации; такой темы в базе нет
вовсе — модель должна была отказать.

## 9. Что ушло дежурному: вопросы без уверенного ответа

Если поиск не нашёл фрагмента с баллом выше порога `RAG_MIN_SCORE`, модель не
вызывается, а строка помечается `escalated = true`. Здесь два типа вопросов:
пробелы базы (кандидаты в новые runbook'и) и слишком короткие вопросы по базе —
на отложенном наборе оценки сюда ушли 8 из 10 таких ([eval.md](eval.md)).

```sql
SELECT ts, left(question, 80) AS question
FROM ask_log
WHERE escalated AND ts > now() - interval '7 days'
ORDER BY ts DESC;
```

## Плохой ответ → кандидат в тестовый набор

Оценка качества ([eval.md](eval.md)) идёт на фиксированном наборе
`eval/questions.jsonl`. Вопросы с 👎 выгружаются заготовками в том же формате:

```bash
docker compose exec -T postgres psql -U facade -d asklog -At -c "
  SELECT json_build_object('id', 'fb' || id, 'kind', NULL, 'runbooks', '[]'::json,
                           'question', question, 'reference', '',
                           'facts', '[]'::json, 'feedback_comment', feedback_comment)
  FROM ask_log WHERE rating = -1 AND feedback_ts > now() - interval '7 days'
  ORDER BY id" > eval/candidates.jsonl
```

Дальше вручную: `kind` (`in` — ответ есть в корпусе, `out` — нет, ждём
отказа), `runbooks`, эталон `reference` и ключевые `facts`; готовые строки
переносятся в `eval/questions.jsonl`. Так каждый пойманный провал становится
регрессионным тестом: следующий прогон покажет, починила ли его правка.

## Миграции

`db/init.sql` выполняется только при первом старте на пустом томе. Новые колонки
на уже работающем стенде добавляют миграции (повторный запуск безопасен):

```bash
docker compose exec -T postgres psql -U facade -d asklog < db/migrations/002_feedback.sql   # оценки 👍/👎
docker compose exec -T postgres psql -U facade -d asklog < db/migrations/003_escalated.sql  # флаг эскалации
```

## Устойчивость к отказу БД

Журнал — вспомогательный слой: если PostgreSQL лежит, `/ask` продолжает
отвечать, а фасад пишет `WARNING ask_log insert failed: ...` в лог (виден в
Loki: `{job="facade"} | json | level="WARNING"`). Проверяется одной командой:
`docker compose stop postgres` → `/ask` по-прежнему 200 → `start` — записи
возобновляются.
