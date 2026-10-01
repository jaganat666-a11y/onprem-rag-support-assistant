-- Миграция 003: флаг эскалации дежурному (порог RAG_MIN_SCORE, docs/eval.md).
-- Балл реранка у лучшего фрагмента ниже порога -> модель не зовём, отвечаем
-- «в runbook'ах нет», строка журнала помечается escalated = true.
-- Повторный запуск безопасен (IF NOT EXISTS).
--
-- Применить:
--   docker compose exec -T postgres psql -U facade -d asklog < db/migrations/003_escalated.sql

ALTER TABLE ask_log
    ADD COLUMN IF NOT EXISTS escalated boolean NOT NULL DEFAULT false;
