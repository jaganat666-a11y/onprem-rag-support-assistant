# -*- coding: utf-8 -*-
"""Нарезка настоящего корпуса runbook'ов — без моделей."""
from collections import Counter

from rag import rag

SECTIONS = {"Симптом", "Область и влияние", "Диагностика", "Решение", "Эскалация", "Связано"}


def test_corpus_cut_into_sections():
    chunks = rag.load_chunks()
    by_rb = Counter(source for source, _, _ in chunks)
    assert len(by_rb) == 16
    # каждый runbook — шесть одинаковых секций; блоки кода внутри секции не рвутся
    for source in by_rb:
        assert {h for s, h, _ in chunks if s == source} == SECTIONS, source
    assert len(chunks) == 16 * len(SECTIONS)


def test_source_has_runbook_id():
    for source, _, _ in rag.load_chunks():
        assert source.startswith("RB-"), source


def test_parse_frontmatter():
    meta, body = rag.parse_frontmatter("---\nid: RB-01\ntitle: Вебхук\n---\n# Заголовок\n")
    assert meta == {"id": "RB-01", "title": "Вебхук"}
    assert body.strip() == "# Заголовок"


def test_context_whole_runbooks_in_document_order(monkeypatch):
    monkeypatch.setattr(rag, "CTX_RUNBOOKS", 2)
    monkeypatch.setattr(rag, "MIN_SCORE", 0.3)
    chunks = rag.load_chunks()
    idx = {}  # source -> индексы его секций по порядку документа
    for i, (s, _, _) in enumerate(chunks):
        idx.setdefault(s, []).append(i)
    rb01, rb05, rb09 = (next(s for s in idx if s.startswith(p)) for p in ("RB-01", "RB-05", "RB-09"))
    # поиск: «Решение» и «Диагностика» RB-05 первыми, RB-01 выше порога, RB-09 — ниже
    scored = [(0.9, idx[rb05][3]), (0.8, idx[rb05][2]), (0.5, idx[rb01][0]), (0.05, idx[rb09][0])]
    context, sources = rag.build_context(scored, chunks)
    assert sources == [rb05, rb01]
    assert f"[{rb09}]" not in context
    # первый runbook — целиком и по порядку документа, без «Связано»
    part = context.split(f"[{rb01}]")[0]
    heads = [line[3:] for line in part.splitlines() if line.startswith("## ")]
    assert heads == ["Симптом", "Область и влияние", "Диагностика", "Решение", "Эскалация"]


def test_context_at_most_ctx_runbooks(monkeypatch):
    monkeypatch.setattr(rag, "CTX_RUNBOOKS", 2)
    monkeypatch.setattr(rag, "MIN_SCORE", 0.3)
    chunks = rag.load_chunks()
    firsts = [i for i, c in enumerate(chunks) if c[1] == "Симптом"][:3]
    _, sources = rag.build_context([(0.9, firsts[0]), (0.8, firsts[1]), (0.7, firsts[2])], chunks)
    assert len(sources) == 2


def test_mark_unverified_lists_lines_missing_from_context():
    context = ("## Решение\n```sql\nSELECT * FROM webhook_events  WHERE id = 1;\n```\n"
               "```bash\ndocker compose restart vllm   # restart the model\n```")
    block = ("```sql\nSELECT * FROM webhook_events WHERE id = 1;\n-- комментарий\n"
             "INSERT INTO ledger VALUES (1);\n```")
    text = f"1. Проверить:\n{block}\n2. Перезапустить:\n```bash\n" \
           f"docker compose restart vllm  # перезапуск модели\n```\nГотово."
    out, n = rag.mark_unverified(text, context)
    # выдуманный INSERT помечен под своим блоком; блок не тронут; переведённый
    # комментарий и лишние пробелы выдумкой не считаются
    assert n == 1
    assert f"{block}\n{rag.UNVERIFIED}\n- `INSERT INTO ledger VALUES (1);`\n2." in out
    assert out.count(rag.UNVERIFIED) == 1


RB_CODE = ("```bash\ndocker exec -it redis redis-cli DEL balance:user:10042\n"
           "docker logs balance-api --since 2m | grep \"Redis connected\"\n```\n"
           "```sql\nSELECT pg_terminate_backend(<blocking_pid>);\n"
           "SELECT status FROM transactions WHERE id = 'tx_8f3ac1';\n```\n"
           "Затем `docker restart redis`.")


def test_placeholder_matches_example_value_only():
    ref = rag.reference(RB_CODE)
    # подстановка вместо значения из примера и та же подстановка — не выдумка
    assert rag.line_in("docker exec -it redis redis-cli DEL balance:user:<user_id>", ref, "")
    assert rag.line_in("SELECT pg_terminate_backend(<blocking_pid>);", ref, "")
    # другая подстановка на месте подстановки — выдумка: можно убить не тот процесс
    assert not rag.line_in("SELECT pg_terminate_backend(<blocked_pid>);", ref, "")
    assert not rag.line_in("SELECT pg_terminate_backend(<pid>);", ref, "")
    # остальная строка по-прежнему дословно
    assert not rag.line_in("docker exec -it redis redis-cli FLUSHALL <user_id>", ref, "")
    assert not rag.line_in("docker exec -it redis redis-cli DEL session:user:<user_id>", ref, "")


def test_truncated_command_marked_split_command_not():
    ref = rag.reference(RB_CODE)
    cut = "docker logs balance-api --since 2m"   # без | grep — часть команды
    assert not rag.line_in(cut, ref, cut)
    # длинный SQL, разбитый на строки, — целая команда, собранная в блоке
    block = rag.norm("SELECT status\nFROM transactions\nWHERE id = 'tx_8f3ac1';")
    assert rag.line_in("SELECT status", ref, block)
    assert not rag.line_in("SELECT status", ref, "SELECT status FROM transactions")


def test_inline_commands_checked_guard_list_and_quotes_not():
    answer = ("Перезапустите `docker restart redis`, затем `docker system prune -af`; "
              "смотрите `cnt > 1`.")
    out, n = rag.mark_unverified(answer, RB_CODE)
    assert n == 1  # выдуманная инлайн-команда; цитата `cnt > 1` — не команда
    assert out.endswith(f"{rag.UNVERIFIED}\n- `docker system prune -af`")
    # повторная проверка не принимает список ⚠️ сторожа за инлайн-команды
    assert rag.inline_commands(out) == ["docker restart redis", "docker system prune -af"]
    assert rag.mark_unverified(out, RB_CODE)[1] == 1


def test_unclosed_last_block_checked_and_closed():
    # ответ обрезан по max_tokens посреди блока кода
    answer = "Выполнить:\n```bash\ndocker exec -it redis redis-cli DEL balance:us"
    out, n = rag.mark_unverified(answer, RB_CODE)
    assert n == 1
    assert "balance:us\n```\n" + rag.UNVERIFIED in out


def test_no_answer_suggests_unique_runbooks():
    hits = [{"source": s} for s in ("RB-02", "RB-02", "RB-05", "RB-06", "RB-07")]
    text = rag.no_answer(hits)
    assert text.startswith(rag.NO_ANSWER)
    assert text.count("- RB-") == rag.SUGGEST_N
    assert "- RB-07" not in text
