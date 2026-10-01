# Проверка команд: 2026-10-01_1249_regen_guard

Каждая строка из блоков кода в ответе ищется дословно в тексте runbook'ов (пробелы схлопнуты, комментарии пропущены). Скрипт `eval/check_commands.py`, без модели.

Ответов с блоками кода: 32 из 50; строк команд: 263; не найдено в корпусе: **5** (в 5 ответах).
Ответов, где все команды есть в корпусе, но часть — из другого runbook'а: 0.
Помечено ⚠️ сторожем фасада (нет в контексте модели): 5 строк в 5 ответах.

## Строки, которых нет в корпусе

| id | строка |
|---|---|
| q06 | `docker exec laravel-app php artisan sbp:refund --txn=tx_8f3ac1` |
| q11 | `ALTER TABLE webhook_events ADD CONSTRAINT unique_idempotency_key UNIQUE (idempotency_key);` |
| q12 | `ALTER TABLE webhook_events ADD CONSTRAINT unique_idempotency_key UNIQUE (idempotency_key);` |
| q16 | `docker exec laravel-backend php artisan webhooks:test --hmac=<new_hmac>` |
| q24 | `{job="gpu_exporter"} \|= "error" \| json \| line_format "{{.ts}} {{.level}} {{.msg}}"` |

## Строки из другого runbook'а

| id | строка | найдена в |
|---|---|---|
