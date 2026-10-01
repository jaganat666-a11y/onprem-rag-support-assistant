# -*- coding: utf-8 -*-
"""
Проверка команд без модели: каждая строка из блоков кода и каждая инлайн-команда
(`docker …`, `SELECT …`) в ответе сверяется с текстом runbook'ов (пробелы
схлопываются, регистр сохраняется).

Ловит то, что судья-модель может пропустить: выдуманную команду, флаг или имя
таблицы. Отдельно отмечает строки, взятые не из того runbook'а, на который
рассчитан вопрос (диагностика из соседнего runbook'а).

Строки-комментарии (# …, -- …, // …), строки, начинающиеся с русского слова
(модель иногда кладёт в блок кода обычный текст), и пустые не проверяются.
Хвостовой комментарий ` # …` отрезается, если строка целиком не нашлась: модель
переводит комментарии из runbook'ов на русский. Строка блока кода должна совпасть
с целой строкой команды runbook'а; часть команды (без флага или условия) — обрезанная
команда, если целиком команда в блоке ответа не собрана. Строка, которой среди команд
нет, но которая есть в тексте runbook'а (пример лога из комментария), — цитата.
Подстановка `<…>` — только вместо конкретного значения или та же самая.
Пропущенные шаги и предупреждения так не ловятся — только добавленные и изменённые строки.

Разбор строк общий с фасадом (rag.command_lines / rag.line_in): фасад сверяет их с
контекстом модели и помечает выдуманные ⚠️ прямо в ответе, а этот скрипт — с
корпусом целиком. Пометка стоит вне блока кода, поэтому помеченные строки здесь
по-прежнему считаются ненайденными.

Запуск:
  py eval/check_commands.py eval/runs/2026-10-01_1006_cite_code
Результат — commands.jsonl и commands.md в папке прогона.
"""
import argparse
import glob
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORPUS_GLOB = os.path.join(ROOT, "corpus", "runbooks", "*.md")
sys.path.insert(0, ROOT)
from rag.rag import command_lines, line_in, reference  # noqa: E402 — после sys.path

ID_RX = re.compile(r"^id:\s*(RB-\d+)", re.M)


def cell(s):
    """Строка в ячейке markdown-таблицы: | внутри ломает колонки."""
    return s.replace("|", r"\|")


def load_corpus():
    """{RB-NN: эталон runbook'а для line_in}"""
    out = {}
    for fp in sorted(glob.glob(CORPUS_GLOB)):
        with open(fp, encoding="utf-8") as f:
            text = f.read()
        m = ID_RX.search(text)
        out[m.group(1) if m else os.path.basename(fp)] = reference(text)
    return out


def find(line, block, corpus):
    return [rb for rb, ref in corpus.items() if line_in(line, ref, block)]


def check(answer, corpus, expected):
    lines = []
    for line, block in command_lines(answer):
        where = find(line, block, corpus)
        lines.append({"line": line, "found_in": where,
                      "status": "missing" if not where else
                                "expected" if set(where) & set(expected) else "other"})
    return lines


def main():
    ap = argparse.ArgumentParser(description="Команды из ответов против текста runbook'ов")
    ap.add_argument("run_dir")
    args = ap.parse_args()
    run_dir = os.path.abspath(args.run_dir)

    corpus = load_corpus()
    with open(os.path.join(run_dir, "answers.jsonl"), encoding="utf-8") as f:
        answers = [json.loads(x) for x in f if x.strip()]

    rows = []
    for a in answers:
        if a.get("status") != 200:
            continue
        lines = check(a.get("answer", ""), corpus, a.get("runbooks") or [])
        rows.append({"id": a["id"], "kind": a["kind"], "lines": lines,
                     "missing": sum(x["status"] == "missing" for x in lines),
                     "other": sum(x["status"] == "other" for x in lines),
                     "marked": a.get("unverified")})  # пометил фасад; None — прогон до сторожа

    with open(os.path.join(run_dir, "commands.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    with_code = [r for r in rows if r["lines"]]
    n_lines = sum(len(r["lines"]) for r in rows)
    n_missing = sum(r["missing"] for r in rows)
    bad = [r for r in rows if r["missing"]]
    foreign = [r for r in rows if r["other"] and not r["missing"]]
    md = [
        f"# Проверка команд: {os.path.basename(run_dir)}",
        "",
        "Каждая строка блоков кода и каждая инлайн-команда ответа сверяется с runbook'ами: "
        "строка кода — с целой строкой команды (часть команды — обрезанная), инлайн — с текстом "
        "(пробелы схлопнуты, комментарии пропущены). Скрипт `eval/check_commands.py`, без модели.",
        "",
        f"Ответов с командами: {len(with_code)} из {len(rows)}; строк команд: {n_lines}; "
        f"не найдено в корпусе: **{n_missing}** (в {len(bad)} ответах).",
        f"Ответов, где все команды есть в корпусе, но часть — из другого runbook'а: {len(foreign)}.",
    ]
    if any(r["marked"] is not None for r in rows):
        n_marked = sum(r["marked"] or 0 for r in rows)
        md.append(f"Помечено ⚠️ сторожем фасада (нет в контексте модели): {n_marked} строк "
                  f"в {sum(1 for r in rows if r['marked'])} ответах.")
    md += [
        "",
        "## Строки, которых нет в корпусе",
        "",
        "| id | строка |",
        "|---|---|",
    ]
    for r in bad:
        for x in r["lines"]:
            if x["status"] == "missing":
                md.append(f"| {r['id']} | `{cell(x['line'])}` |")
    md += ["", "## Строки из другого runbook'а", "", "| id | строка | найдена в |", "|---|---|---|"]
    for r in rows:
        for x in r["lines"]:
            if x["status"] == "other":
                md.append(f"| {r['id']} | `{cell(x['line'])}` | {', '.join(x['found_in'])} |")
    with open(os.path.join(run_dir, "commands.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(md) + "\n")

    print(f"ответов с командами {len(with_code)}/{len(rows)}, строк {n_lines}, "
          f"не найдено {n_missing} в {len(bad)} ответах: {', '.join(r['id'] for r in bad) or '—'}")
    print(f"из другого runbook'а (без выдуманных): {', '.join(r['id'] for r in foreign) or '—'}")
    if any(r["marked"] is not None for r in rows):
        print(f"помечено сторожем фасада: {sum(r['marked'] or 0 for r in rows)} строк")
    print(f"Отчёт: {os.path.join(run_dir, 'commands.md')}")


if __name__ == "__main__":
    main()
