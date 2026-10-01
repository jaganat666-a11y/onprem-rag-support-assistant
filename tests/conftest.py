# -*- coding: utf-8 -*-
"""Общие заглушки для тестов фасада: без GPU, без vLLM, без моделей и без БД.

Эмбеддер и реранкер (sentence-transformers, ~2 ГБ весов) в тестах не грузятся:
индекс и поиск подменяются, генерация в vLLM — тоже. Проверяется то, что делает
сам фасад: коды ответов, порог уверенности, ссылка на runbook, журнал.
"""
import numpy as np
import pytest
from fastapi.testclient import TestClient

from facade import app as facade
from rag import rag

# два runbook'а по две секции: формат (source, header, text), как у rag.load_chunks()
CHUNKS = [
    ("RB-01 — Платёжный вебхук", "Решение", "Повторить доставку вебхука."),
    ("RB-01 — Платёжный вебхук", "Диагностика", "Проверить webhook_events."),
    ('RB-03 — "Автоконвертация RUB→USD"', "Решение", "Переключить провайдера курса."),
    ('RB-03 — "Автоконвертация RUB→USD"', "Симптом", "Платёж завис."),
]

DEAD_URL = "http://127.0.0.1:9/v1"  # порт 9 (discard): соединение отклоняется сразу


@pytest.fixture
def client(monkeypatch):
    """Фасад с подменённым RAG-слоем. По умолчанию: vLLM недоступен, журнал выключен."""
    monkeypatch.setattr(rag, "load_chunks", lambda: list(CHUNKS))
    monkeypatch.setattr(rag, "build_index", lambda chunks, verbose=True: np.zeros((len(chunks), 4)))
    monkeypatch.setattr(rag, "USE_RERANKER", False)  # не прогревать кросс-энкодер на старте
    monkeypatch.setattr(rag, "BASE", DEAD_URL)
    monkeypatch.setattr(facade, "PG_DSN", "")
    monkeypatch.setitem(facade._PG, "conn", None)
    with TestClient(facade.app) as c:  # with — запускает lifespan (загрузку индекса)
        yield c
    facade.STATE.update(ready=False, chunks=None, mat=None, n_chunks=0)
