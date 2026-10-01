-- Миграция 002: оценка ответов 👍/👎 (POST /feedback). 001 — исходная схема, db/init.sql.
-- init.sql выполняется только на ПУСТОМ томе postgres-data, поэтому на уже
-- работающем стенде новые колонки добавляет эта миграция. Повторный запуск
-- безопасен: IF NOT EXISTS пропускает уже существующие колонки.
--
-- Применить:
--   docker compose exec -T postgres psql -U facade -d asklog < db/migrations/002_feedback.sql

ALTER TABLE ask_log
    ADD COLUMN IF NOT EXISTS rating           smallint CHECK (rating IN (1, -1)),
    ADD COLUMN IF NOT EXISTS feedback_comment text,
    ADD COLUMN IF NOT EXISTS feedback_ts      timestamptz;
