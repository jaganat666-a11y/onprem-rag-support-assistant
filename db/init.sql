-- Журнал запросов /ask — SQL-слой стенда.
-- Выполняется АВТОМАТИЧЕСКИ при первом старте контейнера postgres на пустом
-- томе postgres-data (механизм docker-entrypoint-initdb.d). Одна строка
-- таблицы = один обработанный запрос /ask (успешный или упавший).
-- Диагностические запросы дежурного — в docs/sql.md.

CREATE TABLE IF NOT EXISTS ask_log (
    id                bigserial   PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),  -- когда пришёл запрос
    question          text        NOT NULL,                -- сам вопрос
    status            smallint    NOT NULL,                -- HTTP-код ответа: 200 / 502 / ...
    latency_ms        real        NOT NULL,                -- полное время обработки на фасаде
    completion_tokens integer,                             -- токены сгенерированного ответа (NULL при ошибке)
    sources           text[],                              -- ID runbook'ов, ушедших в контекст (NULL при ошибке)
    error             text,                                -- текст ошибки (NULL при успехе)
    rating            smallint    CHECK (rating IN (1, -1)), -- оценка ответа через /feedback: 1 = 👍, -1 = 👎 (NULL — не оценён)
    feedback_comment  text,                                -- комментарий к оценке
    feedback_ts       timestamptz,                         -- когда поставлена оценка
    escalated         boolean     NOT NULL DEFAULT false   -- поиск не уверен: модель не звали, вопрос передан дежурному
);

-- Главные вопросы дежурного — «что было за последний час/сутки»,
-- поэтому индекс по времени, свежие записи первыми.
CREATE INDEX IF NOT EXISTS ask_log_ts_idx ON ask_log (ts DESC);
