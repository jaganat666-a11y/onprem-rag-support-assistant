# -*- coding: utf-8 -*-
"""Поведение фасада без GPU: коды ответов, порог уверенности, ссылка, журнал."""
import pytest

from facade import app as facade
from rag import rag


def ranked(*pairs):
    """Результат rag.retrieve: [(балл реранка, индекс чанка)], метод 'rerank'."""
    return list(pairs), "rerank"


def fake_completion(text="1. Повторить доставку вебхука.", tokens=12):
    def post(path, payload, timeout=600):
        assert path == "/chat/completions"
        return {"choices": [{"message": {"content": text}}],
                "usage": {"completion_tokens": tokens}}
    return post


def model_must_not_be_called(path, payload, timeout=600):
    raise AssertionError("ниже порога уверенности модель вызываться не должна")


# --- / (демо-страница) -----------------------------------------------------

def test_demo_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    # страница ходит в тот же API, что и любой клиент, и ничего не тянет из интернета
    assert 'fetch("/ask"' in r.text and 'fetch("/feedback"' in r.text
    assert "https://" not in r.text


# --- /health ---------------------------------------------------------------

def test_health_503_when_vllm_down(client):
    r = client.get("/health")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "degraded"
    assert body["index_loaded"] is True and body["vllm_reachable"] is False
    assert body["chunks"] == 4


def test_health_200_when_vllm_up(client, monkeypatch):
    monkeypatch.setattr(facade, "_vllm_reachable", lambda timeout=3.0: True)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# --- /ask ------------------------------------------------------------------

def test_ask_empty_question_422(client):
    assert client.post("/ask", json={"question": "   "}).status_code == 422


def test_ask_503_while_index_loading(client):
    facade.STATE["ready"] = False
    assert client.post("/ask", json={"question": "вебхук не пришёл"}).status_code == 503


def test_ask_502_when_vllm_down(client, monkeypatch):
    # поиск уверен -> фасад идёт в vLLM, а тот не отвечает: 502, не 500
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0), (0.5, 1)))
    r = client.post("/ask", json={"question": "вебхук не пришёл"})
    assert r.status_code == 502
    assert "Upstream vLLM unreachable" in r.json()["detail"]


def test_ask_too_long_question_422(client):
    r = client.post("/ask", json={"question": "x" * (facade.QUESTION_MAX + 1)})
    assert r.status_code == 422


def test_ask_vllm_http_error_is_not_unreachable(client, monkeypatch):
    # vLLM доступен, но отклонил запрос (400): ответ честно говорит об ошибке,
    # а не «сервер недоступен»
    import urllib.error

    def post(path, payload, timeout=600):
        raise urllib.error.HTTPError(rag.BASE + path, 400, "Bad Request", None, None)
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0)))
    monkeypatch.setattr(rag, "post", post)
    r = client.post("/ask", json={"question": "вебхук потерялся"})
    assert r.status_code == 502
    assert "HTTP 400" in r.json()["detail"] and "unreachable" not in r.json()["detail"]


def test_ask_vllm_without_choices_502(client, monkeypatch):
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0)))
    monkeypatch.setattr(rag, "post", lambda path, payload, timeout=600: {"error": "x"})
    r = client.post("/ask", json={"question": "вебхук потерялся"})
    assert r.status_code == 502


def test_ask_answer_ends_with_source_from_retrieval(client, monkeypatch):
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0), (0.6, 2)))
    monkeypatch.setattr(rag, "post", fake_completion())
    r = client.post("/ask", json={"question": "вебхук не пришёл"})
    assert r.status_code == 200
    body = r.json()
    assert body["escalate"] is False
    # ссылку ставит код: runbook, найденный первым, — а не всё, что было в контексте
    assert body["answer"].endswith("Источник: RB-01 — Платёжный вебхук")
    assert body["cited"] == ["RB-01 — Платёжный вебхук"]
    assert len(body["sources"]) == 2  # в контексте — оба runbook'а
    assert body["completion_tokens"] == 12
    assert body["ask_id"] is None  # журнал выключен
    assert body["unverified"] == 0


def test_ask_marks_command_not_in_context(client, monkeypatch):
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0)))
    monkeypatch.setattr(rag, "post", fake_completion("1. Повторить:\n```bash\ncurl -X POST /replay\n```"))
    body = client.post("/ask", json={"question": "вебхук не пришёл"}).json()
    assert body["unverified"] == 1
    assert f"{rag.UNVERIFIED}\n- `curl -X POST /replay`" in body["answer"]


def test_ask_below_threshold_escalates_without_model(client, monkeypatch):
    monkeypatch.setattr(rag, "MIN_SCORE", 0.3)
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.1, 2), (0.05, 3), (0.02, 0)))
    monkeypatch.setattr(rag, "post", model_must_not_be_called)
    r = client.post("/ask", json={"question": "лимиты СБП"})
    assert r.status_code == 200
    body = r.json()
    assert body["escalate"] is True
    assert body["answer"].startswith(rag.NO_ANSWER)
    # подсказка: разные runbook'и в порядке реранка, кавычки из YAML убраны
    assert "- RB-03 — Автоконвертация RUB→USD\n- RB-01 — Платёжный вебхук" in body["answer"]
    assert body["completion_tokens"] == 0


def test_ask_survives_db_down(client, monkeypatch):
    # БД недоступна: журнал деградирует в WARNING, ответ всё равно уходит
    monkeypatch.setattr(facade, "PG_DSN", "postgresql://u:p@127.0.0.1:1/db")
    monkeypatch.setattr(rag, "retrieve", lambda q, c, m: ranked((0.9, 0)))
    monkeypatch.setattr(rag, "post", fake_completion())
    r = client.post("/ask", json={"question": "вебхук не пришёл"})
    assert r.status_code == 200
    assert r.json()["ask_id"] is None


# --- /feedback -------------------------------------------------------------

class FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.params = params

    def fetchone(self):
        return self.row


class FakeConn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return FakeCursor(self.row)


@pytest.fixture
def journal(monkeypatch):
    """Журнал «включён», соединение — заглушка; row — что вернёт UPDATE ... RETURNING."""
    monkeypatch.setattr(facade, "PG_DSN", "postgresql://stub")

    def use(row):
        monkeypatch.setattr(facade, "_pg_conn", lambda: FakeConn(row))
    return use


def test_feedback_503_when_journal_off(client):
    assert client.post("/feedback", json={"ask_id": 1, "rating": 1}).status_code == 503


def test_feedback_422_bad_rating(client, journal):
    journal((1,))
    assert client.post("/feedback", json={"ask_id": 1, "rating": 5}).status_code == 422


def test_feedback_404_unknown_ask_id(client, journal):
    journal(None)
    assert client.post("/feedback", json={"ask_id": 999, "rating": -1}).status_code == 404


def test_feedback_ok(client, journal):
    journal((42,))
    r = client.post("/feedback", json={"ask_id": 42, "rating": -1, "comment": "не тот runbook"})
    assert r.status_code == 200
    assert r.json() == {"ask_id": 42, "rating": -1}


def test_feedback_503_when_db_down(client, monkeypatch):
    monkeypatch.setattr(facade, "PG_DSN", "postgresql://u:p@127.0.0.1:1/db")
    assert client.post("/feedback", json={"ask_id": 1, "rating": 1}).status_code == 503
