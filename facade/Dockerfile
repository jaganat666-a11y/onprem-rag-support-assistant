# Тонкий API-фасад над RAG-слоем: HTTP /ask + /health.
# Образ намеренно CPU-only — GPU нужен только vLLM (он отдельным контейнером),
# а эмбеддер/реранкер фасада крутятся на CPU, генерация уходит в vLLM по HTTP.
#
# Build context = КОРЕНЬ репозитория (см. docker-compose: build.context: .),
# чтобы COPY дотягивался до rag/ и corpus/. Веса bge-m3/reranker в образ НЕ
# пекутся (это ~2.3 ГБ) — качаются в том HF-кеша на первом старте.
FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface \
    HF_HUB_DISABLE_PROGRESS_BARS=1
# ^ HF_HUB_DISABLE_PROGRESS_BARS: прогрессбары скачивания весов bge — НЕ-JSON в
#   stderr; глушим, чтобы поток логов был единым JSON под Promtail -> Loki.

WORKDIR /app

# Зависимости. torch ставим CPU-сборкой ЯВНО до sentence-transformers, иначе
# подтянется CUDA-колесо (~2 ГБ лишнего). Когда torch уже удовлетворён,
# sentence-transformers его не переустанавливает.
COPY facade/requirements.txt /app/facade/requirements.txt
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0 \
 && pip install -r /app/facade/requirements.txt

# Код фасада + RAG-слой + корпус runbook'ов (тот же layout, что на хосте:
# rag.py вычисляет ROOT как два уровня вверх -> /app, и находит /app/corpus).
COPY rag/ /app/rag/
COPY corpus/ /app/corpus/
COPY facade/ /app/facade/

EXPOSE 8080
# Один воркер: индекс и модели грузятся в память один раз на процесс.
# Масштабирование — горизонтально (несколько реплик за балансировщиком).
# --no-access-log: per-request лог пишет middleware приложения (структурный JSON),
# дублирующий текстовый access-лог uvicorn выключаем — поток логов единый под Loki.
CMD ["uvicorn", "facade.app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
