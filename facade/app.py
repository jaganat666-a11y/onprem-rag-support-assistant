# -*- coding: utf-8 -*-
"""
Тонкий HTTP-фасад над RAG-слоем (rag/rag.py).

Эндпоинты:
  GET  /         — демо-страница чата (facade/demo.html): вопрос, ответ с источником,
                   жёлтая плашка эскалации, 👍/👎. Ходит в те же /ask и /feedback.
  GET  /health   — жив ли сервис: индекс runbook'ов загружен И vLLM отвечает.
                   200 если оба условия выполнены, иначе 503 (Service Unavailable).
  POST /ask      — вопрос по корпусу runbook'ов. Прогоняет ВЕСЬ RAG-путь
                   (bge-m3 -> 12 кандидатов -> реранк -> порог -> до двух runbook'ов -> vLLM) и
                   возвращает ответ с источниками и ask_id. Если vLLM недоступен — 502.
  POST /feedback — оценка ответа 👍/👎 по ask_id: пишется в ту же строку ask_log.
                   Плохие ответы достаются SQL и становятся кандидатами в
                   тестовый набор eval/questions.jsonl (docs/sql.md).

Фасад НЕ дублирует логику RAG: он импортирует функции из rag/rag.py
(load_chunks / build_index / get_reranker / answer) и оборачивает их в HTTP.

GPU фасаду НЕ нужен: эмбеддер bge-m3 и реранкер bge-reranker-v2-m3 работают на
CPU, генерацию фасад шлёт в vLLM по HTTP. Адрес vLLM — env VLLM_BASE
(по умолчанию http://localhost:8000/v1 для запуска на хосте; в контейнере
docker-compose задаёт http://vllm:8000/v1 — сервис vllm).

Модели и индекс грузятся ОДИН раз при старте (~10–70 с на CPU) — поэтому в
docker-compose заложен healthcheck.start_period: 120s.

Логи — структурированный JSON в stdout (ts/level/msg + контекст запроса), готов
к сбору Promtail -> Loki под меткой job="facade". Точный поиск ошибок —
по полю уровня, а не по подстроке:
  {job="facade"} | json | level="ERROR"

Журнал запросов (SQL-слой): каждый обработанный /ask пишется строкой в
PostgreSQL (таблица ask_log, схема — db/init.sql, диагностика — docs/sql.md).
Журнал вспомогательный: если БД недоступна, /ask продолжает работать, а ошибка
записи уходит WARNING'ом в лог. Пустой PG_DSN = журналирование выключено
(запуск на хосте без БД).
"""
import json
import logging
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

# --- импорт RAG-слоя из соседней папки rag/ -------------------------------
# Фасад лежит в <root>/facade/app.py, RAG — в <root>/rag/rag.py. Кладём корень
# репозитория в sys.path, чтобы работал `from rag import rag` и на хосте, и в
# контейнере (там тот же layout: /app/facade, /app/rag, /app/corpus).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from rag import rag  # noqa: E402  (импорт после правки sys.path — это намеренно)

import psycopg2  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


# --- структурированное JSON-логирование (под Loki) ------------------------
_LOG_EXTRA_KEYS = ("method", "path", "status", "latency_ms", "client", "chunks")


class JsonLogFormatter(logging.Formatter):
    """Одна строка лога = один JSON-объект. Поля ts/level/logger/msg всегда есть;
    контекст запроса (method/path/status/...) подкладывается через logging extra."""

    def format(self, record):
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _LOG_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _setup_logging():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # uvicorn ставит СВОИ хендлеры (НЕ-JSON: "INFO: Started server process ...") на
    # собственные логгеры с propagate=False. Этот модуль импортируется ПОСЛЕ того, как
    # uvicorn сконфигурировал логирование, поэтому здесь мы перенаправляем его логгеры
    # на наш JSON-хендлер — тогда ВЕСЬ stdout = один JSON-формат (ts/level/logger/msg),
    # и Promtail -> Loki забирает поток единообразно (без смеси JSON и текста).
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = [handler]
        lg.propagate = False
    # access-логи uvicorn дублируют middleware log_requests (оно уже пишет
    # method/path/status/latency_ms в JSON) -> глушим их (плюс --no-access-log в CMD).
    acc = logging.getLogger("uvicorn.access")
    acc.handlers[:] = []
    acc.propagate = False


_setup_logging()
log = logging.getLogger("facade")


# --- состояние процесса: индекс грузится один раз при старте ---------------
STATE = {"ready": False, "chunks": None, "mat": None, "n_chunks": 0}


# --- журнал запросов в PostgreSQL (SQL-слой) --------------------------------
# Одно ленивое соединение на процесс: открывается при первой записи,
# пересоздаётся после любой ошибки (протухший коннект, рестарт БД).
PG_DSN = os.environ.get("PG_DSN", "")
_PG = {"conn": None}


def _pg_conn():
    if _PG["conn"] is None or _PG["conn"].closed:
        _PG["conn"] = psycopg2.connect(PG_DSN, connect_timeout=3)
        _PG["conn"].autocommit = True
    return _PG["conn"]


def log_ask(question, status, latency_ms, tokens=None, sources=None, error=None,
            escalated=False):
    """Одна строка ask_log = один обработанный /ask (и успех, и ошибка).
    Возвращает id строки — по нему клиент потом ставит оценку (/feedback);
    None, если журнал выключен или запись не удалась.
    Журнал вспомогательный: падение БД НЕ должно ронять /ask, поэтому любая
    ошибка записи глотается и уходит WARNING'ом в лог (его поймает Loki)."""
    if not PG_DSN:
        return None
    try:
        with _pg_conn().cursor() as cur:
            cur.execute(
                "INSERT INTO ask_log"
                " (question, status, latency_ms, completion_tokens, sources, error, escalated)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (question, status, latency_ms, tokens, sources, error, escalated),
            )
            return cur.fetchone()[0]
    except Exception as e:
        _PG["conn"] = None
        log.warning("ask_log insert failed: %s", e)
        return None


def _vllm_reachable(timeout=3.0):
    """Дёшево проверяем, что vLLM отвечает: GET {VLLM_BASE}/models.
    Любая ошибка соединения/таймаут/не-2xx -> считаем апстрим недоступным."""
    url = rag.BASE.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


@asynccontextmanager
async def lifespan(_app):
    # старт: читаем корпус, строим индекс (bge-m3 CPU), прогреваем реранкер.
    # Блокирующая загрузка — uvicorn начнёт принимать запросы только после неё;
    # на прогреве healthcheck в start_period терпит отказ соединения.
    log.info("startup: загружаю корпус и строю индекс (bge-m3, CPU)...")
    t0 = time.perf_counter()
    chunks = rag.load_chunks()
    mat = rag.build_index(chunks, verbose=False)
    if rag.USE_RERANKER:
        rag.get_reranker()  # прогрев кросс-энкодера, чтобы первый /ask не ждал загрузку
    STATE.update(ready=True, chunks=chunks, mat=mat, n_chunks=len(chunks))
    log.info("startup: готов", extra={"chunks": len(chunks),
                                      "latency_ms": round((time.perf_counter() - t0) * 1000, 1)})
    yield
    log.info("shutdown")


app = FastAPI(title="onprem-rag support facade", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def log_requests(request, call_next):
    """Одна JSON-строка на запрос: метод, путь, код ответа, latency. Этот поток
    stdout и забирает Promtail в Loki."""
    t0 = time.perf_counter()
    response = await call_next(request)
    log.info("request", extra={
        "method": request.method,
        "path": request.url.path,
        "status": response.status_code,
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "client": request.client.host if request.client else None,
    })
    return response


# Вопрос дежурного — пара строк. Лимит держит запрос в контексте модели (8192 токена,
# из них до ~3,2 тыс. — два runbook'а); длинный текст vLLM отклонил бы ошибкой 400.
QUESTION_MAX = 2000


class AskRequest(BaseModel):
    question: str = Field(max_length=QUESTION_MAX)


class FeedbackRequest(BaseModel):
    ask_id: int
    rating: Literal[1, -1]                                   # 1 = 👍, -1 = 👎
    comment: Optional[str] = Field(default=None, max_length=2000)


DEMO_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo.html")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo():
    """Демо-страница чата: статичный HTML без внешних зависимостей (стенд on-prem).
    Логики в ней нет — только вызовы /ask, /feedback и /health из браузера."""
    with open(DEMO_HTML, encoding="utf-8") as f:
        return f.read()


@app.get("/health")
def health():
    """200, если индекс загружен И vLLM достижим; иначе 503. Так дежурный
    отличает «фасад жив, но vLLM лёг» (503) от «фасада вообще нет» (нет ответа)."""
    index_ok = STATE["ready"]
    vllm_ok = _vllm_reachable()
    ok = index_ok and vllm_ok
    body = {
        "status": "ok" if ok else "degraded",
        "index_loaded": index_ok,
        "vllm_reachable": vllm_ok,
        "chunks": STATE["n_chunks"],
        # текущие параметры поиска (env RAG_*) — прогон eval/ записывает их в итог
        "retrieval": {"rerank": rag.USE_RERANKER, "cand_k": rag.CAND_K, "top_k": rag.TOP_K,
                      "min_score": rag.MIN_SCORE, "ctx_runbooks": rag.CTX_RUNBOOKS},
    }
    if not ok:
        return JSONResponse(status_code=503, content=body)
    return body


@app.post("/ask")
def ask(req: AskRequest):
    """Полный RAG-путь: вопрос -> bge-m3 -> 12 кандидатов -> реранк -> порог -> vLLM -> сторож."""
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="Поле 'question' пустое")
    if not STATE["ready"]:
        raise HTTPException(status_code=503, detail="Индекс ещё загружается, повторите позже")
    t0 = time.perf_counter()
    try:
        r = rag.answer(question, STATE["chunks"], STATE["mat"])
    except urllib.error.HTTPError as e:
        # vLLM ответил, но с ошибкой. HTTPError — подкласс URLError, поэтому ловится
        # раньше «недоступен»: 4xx — запрос не принят (например, не влез в контекст),
        # 5xx — сбой самого сервера модели.
        log.error("vLLM HTTP %s", e.code)
        log_ask(question, 502, round((time.perf_counter() - t0) * 1000, 1),
                error=f"vLLM HTTP {e.code}")
        raise HTTPException(status_code=502, detail=f"vLLM вернул ошибку HTTP {e.code}")
    except ValueError as e:
        log.error("vLLM bad response: %s", e)
        log_ask(question, 502, round((time.perf_counter() - t0) * 1000, 1), error=str(e))
        raise HTTPException(status_code=502, detail="vLLM вернул ответ без текста")
    except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError) as e:
        # vLLM не ответил — фасад честно отдаёт 502 (формулировка из RB-11).
        reason = getattr(e, "reason", e)
        log.error("upstream vLLM error: %s", reason)
        log_ask(question, 502, round((time.perf_counter() - t0) * 1000, 1),
                error=str(reason))
        raise HTTPException(status_code=502,
                            detail=f"Upstream vLLM unreachable: {reason} ({rag.BASE})")
    ask_id = log_ask(question, 200, round((time.perf_counter() - t0) * 1000, 1),
                     tokens=r["completion_tokens"], sources=r["sources"],
                     escalated=r["escalate"])
    return {
        "ask_id": ask_id,                           # для /feedback; null, если журнал недоступен
        "answer": r["answer"],
        # поиск не уверен (балл ниже RAG_MIN_SCORE): модель не звали, вопрос — дежурному
        "escalate": r["escalate"],
        "sources": r["sources"],                    # runbook'и, ушедшие в контекст
        "cited": r.get("cited", []),                # runbook, названный в ответе (ставит код)
        "retrieval": r["retrieval"],
        "latency_s": round(r["gen_s"], 2),          # генерация в vLLM (как и раньше)
        "retrieval_s": round(r["retrieval_s"], 2),  # поиск: эмбеддинг + реранк на CPU
        "completion_tokens": r["completion_tokens"],
        # строки блоков кода, которых нет в контексте: помечены в ответе ⚠️ (rag.mark_unverified)
        "unverified": r.get("unverified", 0),
        "scores": [h["score"] for h in r["hits"]],
        "chunks": r["hits"],                        # найденные фрагменты в порядке реранка
    }


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    """Оценка ответа 👍/👎: пишется в строку ask_log, созданную тем /ask.
    Повторная оценка того же ответа перезаписывает прежнюю. Оценивать можно
    только успешный ответ (status=200); нет такой строки — 404.
    В отличие от /ask, здесь журнал — не побочный эффект, а сама суть запроса,
    поэтому недоступная БД -> 503, а не молчаливый пропуск."""
    if not PG_DSN:
        raise HTTPException(status_code=503, detail="Журнал запросов выключен (PG_DSN пуст)")
    try:
        with _pg_conn().cursor() as cur:
            cur.execute(
                "UPDATE ask_log SET rating = %s, feedback_comment = %s, feedback_ts = now()"
                " WHERE id = %s AND status = 200 RETURNING id",
                (req.rating, req.comment, req.ask_id),
            )
            row = cur.fetchone()
    except Exception as e:
        _PG["conn"] = None
        log.warning("ask_log feedback update failed: %s", e)
        raise HTTPException(status_code=503, detail="Журнал запросов недоступен")
    if row is None:
        raise HTTPException(status_code=404, detail=f"Ответ ask_id={req.ask_id} не найден")
    log.info("feedback ask_id=%s rating=%s", req.ask_id, req.rating)
    return {"ask_id": req.ask_id, "rating": req.rating}
