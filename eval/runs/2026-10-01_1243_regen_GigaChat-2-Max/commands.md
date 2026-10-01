# Проверка команд: 2026-10-01_1243_regen_GigaChat-2-Max

Каждая строка из блоков кода в ответе ищется дословно в тексте runbook'ов (пробелы схлопнуты, комментарии пропущены). Скрипт `eval/check_commands.py`, без модели.

Ответов с блоками кода: 30 из 50; строк команд: 252; не найдено в корпусе: **10** (в 7 ответах).
Ответов, где все команды есть в корпусе, но часть — из другого runbook'а: 0.
Помечено ⚠️ сторожем фасада (нет в контексте модели): 13 строк в 9 ответах.

## Строки, которых нет в корпусе

| id | строка |
|---|---|
| q02 | `INSERT INTO webhook_events (event_id, txn_id, ...) VALUES ('new-event-id', 'tx_8f3ac1', ...)` |
| q02 | `ON CONFLICT (event_id) DO NOTHING;` |
| q06 | `docker exec laravel-app php artisan sbp:finalize-transaction --txn=tx_8f3ac1 --status=failed` |
| q07 | `curl -v --max-time 5 https://fx-provider-fallback.internal/api/rate?pair=USDRUB` |
| q30 | `sed -i 's/host\.docker\.internal/172.17.112.1/g' prometheus.yml` |
| q31 | `sed -i 's/host\.docker\.internal/172.17.112.1/g' prometheus.yml` |
| q38 | `git add Dockerfile` |
| q38 | `git commit -m "Upgrade PHP version in Dockerfile"` |
| q38 | `git push origin <ваша ветка>` |
| q39 | `docker exec laravel-app php artisan sbp:refund --txn=tx_8f3ac1` |

## Строки из другого runbook'а

| id | строка | найдена в |
|---|---|---|
