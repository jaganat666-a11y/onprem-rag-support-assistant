# Проверка команд: 2026-10-01_1006_cite_code

Каждая строка из блоков кода в ответе ищется дословно в тексте runbook'ов (пробелы схлопнуты, комментарии пропущены). Скрипт `eval/check_commands.py`, без модели.

Ответов с блоками кода: 31 из 50; строк команд: 228; не найдено в корпусе: **8** (в 4 ответах).
Ответов, где все команды есть в корпусе, но часть — из другого runbook'а: 1.

## Строки, которых нет в корпусе

| id | строка |
|---|---|
| q02 | `INSERT INTO webhook_events (event_id, txn_id, status, created_at, processed_at)` |
| q02 | `VALUES ('tx_8f3ac1', 'tx_8f3ac1', 'pending', CURRENT_TIMESTAMP, NULL)` |
| q02 | `ON CONFLICT (event_id) DO NOTHING;` |
| q12 | `ALTER TABLE webhook_events ADD CONSTRAINT uq_idempotency_key UNIQUE (idempotency_key);` |
| q16 | `curl -X POST https://your-api-endpoint.com/webhook` |
| q16 | `-d '{"key": "value"}'` |
| q33 | `{job="promtail"} \|= "promtail_read_bytes_total" \| json \| line_format "{{.ts}} {{.msg}}"` |
| q33 | `{job="promtail"} \|= "promtail_file_bytes_total" \| json \| line_format "{{.ts}} {{.msg}}"` |

## Строки из другого runbook'а

| id | строка | найдена в |
|---|---|---|
| q16 | `-H "Content-Type: application/json"` | RB-04 |
| q32 | `docker ps --filter name=vllm --filter name=facade` | RB-11 |
| q32 | `{job="facade"} \|= "error" \| json \| line_format "{{.ts}} {{.level}} {{.msg}}"` | RB-11 |
| q32 | `2026-06-23T03:14:07Z ERROR httpx._client connect error: [Errno 111] Connection refused ('vllm', 8000)` | RB-11 |
| q32 | `docker logs vllm --tail=100 --since=30m` | RB-11 |
| q32 | `curl -v http://localhost:8000/health` | RB-11 |
| q32 | `ss -tlnp \| grep 8000` | RB-10, RB-11, RB-12 |
| q32 | `SELECT id, created_at, service, message` | RB-11 |
| q32 | `WHERE service = 'ai-assistant-facade'` | RB-11 |
| q32 | `AND created_at > NOW() - INTERVAL '1 hour'` | RB-11 |
| q32 | `LIMIT 10;` | RB-11 |
