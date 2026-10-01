# -*- coding: utf-8 -*-
"""
Судья для прогона оценки: ставит каждому ответу вердикт верно / частично / неверно
и отмечает выдумки и отказы. Итог — grades.jsonl, report.md и лист ручной проверки.

Судья — сильная внешняя модель (по умолчанию Claude Sonnet 5.5 через Claude Code CLI в
режиме `claude -p`, без инструментов и MCP). Модель задаётся точным id, а не алиасом:
алиас `sonnet` со временем указывает на другие модели, и цифры разных прогонов
перестают быть сравнимыми. В отчёт пишется id, который CLI реально вызвал
(`modelUsage`). Он видит ПОЛНЫЙ текст всех runbook'ов,
эталон из набора и ответ ассистента — поэтому может проверить каждую команду и
цифру по источнику, а не только по короткому эталону. Все 50 ответов уходят одним
пакетом: один вызов на прогон.

Почему внешняя модель допустима: корпус и вопросы синтетические. Для реальных
данных судья должен быть локальным, как и сам ассистент.

Запуск:
  py eval/judge.py eval/runs/2026-09-30_1540_qwen2.5-7b
  py eval/judge.py <папка> --model claude-opus-5-5   # другой судья вместо основного
  py eval/judge.py <папка> --model claude-opus-5-5 --second   # второй судья + сверка
  py eval/judge.py <папка> --report-only             # пересобрать отчёт по готовым grades.jsonl

Прежние вердикты при новом вызове судьи не теряются: grades.jsonl и judge_meta.json
переименовываются в grades_<модель>.jsonl / judge_meta_<модель>.json. Второй судья
(--second) сразу пишет в grades_<модель>.jsonl и report_<модель>.md, а основной отчёт,
summary.json и лист ручной проверки не трогает.
"""
import argparse
import glob
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CORPUS_GLOB = os.path.join(ROOT, "corpus", "runbooks", "*.md")

RUBRIC = """Ты — строгий проверяющий ответов RAG-ассистента дежурного инженера поддержки.
Ассистент отвечает по корпусу runbook'ов; полный текст корпуса ниже. Для каждого
ответа поставь вердикт. Сверяй каждую команду, цифру, имя таблицы и метрики с
текстом runbook'ов, а не со своими знаниями.

Вопросы kind=in (ответ в корпусе есть):
- correct — главные действия и факты из эталона есть, противоречий корпусу нет.
  Лишние детали, которые есть в корпусе, — не ошибка; порядок и формулировки не важны.
- partial — направление верное, но пропущен важный шаг или факт из эталона, либо
  есть неточность, которая не ведёт к вредному действию.
- incorrect — не тот runbook или не те действия, противоречие корпусу, выдуманные
  команды или факты, способные навредить, либо отказ, хотя ответ в корпусе есть.

Вопросы kind=out (ответа в корпусе нет):
- correct — ассистент прямо сказал, что в runbook'ах этого нет, и не выдал
  выдуманную процедуру за ответ (сослаться на смежный runbook можно).
- partial — честный отказ, но нарушено правило языка (ниже).
- incorrect — выдал инструкцию или цифры, которых нет в корпусе, как ответ.

Ограничения, которые понижают вердикт независимо от смысла:
- Язык. Дежурный читает по-русски. Если в ответе есть слова или фразы на китайском
  или другом языке (английские технические термины, команды, код, имена метрик и
  таблиц — не в счёт), вердикт не выше partial.
- Ссылки (только kind=in). Если ответ ссылается на runbook («Опираюсь на RB-…»), из
  которого в ответе не взято ни одного шага или факта, вердикт не выше partial:
  дежурный пойдёт читать не тот документ.

Для каждого ответа также:
- hallucination: true, если есть конкретные команды, цифры, имена таблиц или
  метрик, которых НЕТ в корпусе, и они поданы как факт;
- refused: true, если ассистент заявил, что информации нет (целиком или по главной части);
- not_russian: true, если сработало правило языка;
- stray_cite: true, если сработало правило ссылок;
- comment: одно короткое предложение по-русски — что не так (для correct можно пусто).

Ответь ТОЛЬКО JSON-объектом, без пояснений и без markdown:
{"grades": [{"id": "q01", "verdict": "correct", "hallucination": false, "refused": false, "not_russian": false, "stray_cite": false, "comment": ""}]}
Ровно по одному элементу на каждый id из списка, без пропусков."""

# Qwen иногда срывается в китайский, хотя промпт это запрещает. Судья Sonnet 4.6 с
# первой рубрикой такое засчитывал (смысл верный), поэтому язык дополнительно
# проверяется скриптом по тексту — это не зависит от судьи.
CJK_RX = re.compile(r"[一-鿿]")
DEFAULT_JUDGE = "claude-sonnet-5-5"

VERDICTS = ("correct", "partial", "incorrect")
RU = {"correct": "верно", "partial": "частично", "incorrect": "неверно"}


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def corpus_text():
    parts = []
    for fp in sorted(glob.glob(CORPUS_GLOB)):
        with open(fp, encoding="utf-8") as f:
            parts.append(f"----- {os.path.basename(fp)} -----\n{f.read().strip()}")
    return "\n\n".join(parts)


def claude_bin(explicit):
    """Путь к Claude Code CLI. На Windows npm ставит обёртку claude.cmd; вызываем
    лежащий за ней claude.exe напрямую — cmd.exe портит аргументы вроде пустого ""."""
    path = explicit or shutil.which("claude")
    if not path:
        sys.exit("Не найден Claude Code CLI (claude). Укажите путь: --claude-bin")
    if path.lower().endswith(".cmd"):
        exe = os.path.join(os.path.dirname(path), "node_modules", "@anthropic-ai",
                           "claude-code", "bin", "claude.exe")
        if os.path.exists(exe):
            return exe
    return path


def call_judge(prompt, model, binary):
    cmd = [binary, "-p", "--model", model, "--output-format", "json",
           "--tools", "", "--strict-mcp-config", "--no-session-persistence",
           # без пользовательского ~/.claude/CLAUDE.md, навыков и плагинов: CLI
           # подмешивает их даже в `-p` (до 2026-10-01 судьи шли с ними)
           "--safe-mode"]
    # Без этой переменной CLI попутно отдаёт весь промпт мелкой модели на служебную
    # задачу (~50К токенов Haiku на прогон); на вердикты это не влияет.
    env = dict(os.environ, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    # cwd — нейтральная папка: судье не нужны CLAUDE.md и память проекта.
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", cwd=tempfile.gettempdir(), env=env, timeout=1800)
    if proc.returncode != 0:
        sys.exit(f"claude -p завершился с кодом {proc.returncode}:\n{proc.stderr[-2000:]}")
    envelope = json.loads(proc.stdout)
    text = (envelope.get("result") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        sys.exit(f"Судья не вернул JSON:\n{text[:2000]}")
    grades = json.loads(text[start:end + 1])["grades"]
    usage = {k: envelope.get(k) for k in ("total_cost_usd", "duration_ms", "usage", "modelUsage")}
    return grades, usage


def judge_model_id(judge_meta):
    """Какая модель на самом деле судила: из modelUsage берём ту, что написала больше
    всего токенов (CLI попутно зовёт мелкую модель для служебных задач)."""
    mu = (judge_meta.get("usage") or {}).get("modelUsage") or {}
    if not mu:
        return judge_meta.get("model", "?")
    return max(mu, key=lambda k: mu[k].get("outputTokens", 0))


def stray_cites(a):
    """Ссылки на runbook'и, которых нет среди ожидаемых для вопроса (только kind=in).
    Верхняя оценка: скрипт не видит, взят ли из такого runbook'а хоть один шаг."""
    if a["kind"] != "in":
        return []
    return sorted(set(a.get("cited") or []) - set(a["runbooks"]))


def pct(num, den):
    return f"{100 * num / den:.0f}%" if den else "—"


def agreement_lines(answers, primary, second, primary_id, second_id):
    """Сверка двух судей на одних ответах: совпадение вердиктов и разбор расхождений."""
    p = {x["id"]: x for x in primary}
    s = {x["id"]: x for x in second}
    ids = [a["id"] for a in answers if a["id"] in p and a["id"] in s]
    diff = [i for i in ids if p[i]["verdict"] != s[i]["verdict"]]
    rank = {v: n for n, v in enumerate(VERDICTS)}
    stricter = sum(1 for i in diff if rank.get(s[i]["verdict"], 0) > rank.get(p[i]["verdict"], 0))
    hall = [i for i in ids if bool(p[i].get("hallucination")) != bool(s[i].get("hallucination"))]
    lines = [
        "", f"## Сверка с основным судьёй `{primary_id}`", "",
        f"Вердикты совпали: **{len(ids) - len(diff)}/{len(ids)}**. Из {len(diff)} расхождений "
        f"`{second_id}` строже в {stricter}, мягче в {len(diff) - stricter}. "
        f"Пометка «выдумка» расходится в {len(hall)}: {', '.join(hall) or '—'}.",
        "", f"| id | `{primary_id}` | `{second_id}` | комментарий `{second_id}` |", "|---|---|---|---|",
    ]
    for i in diff:
        lines.append(f"| {i} | {RU.get(p[i]['verdict'], p[i]['verdict'])} | "
                     f"{RU.get(s[i]['verdict'], s[i]['verdict'])} | "
                     f"{s[i].get('comment', '').replace('|', '/')} |")
    return lines


def build_report(run_dir, answers, questions, grades, summary, judge_meta,
                 name="report.md", extra=()):
    qmap = {q["id"]: q for q in questions}
    g = {x["id"]: x for x in grades}
    ins = [a for a in answers if a["kind"] == "in" and a["status"] == 200]
    outs = [a for a in answers if a["kind"] == "out" and a["status"] == 200]

    def count(rows, pred):
        return sum(1 for a in rows if a["id"] in g and pred(g[a["id"]]))

    def cjk(a):
        return bool(CJK_RX.search(a.get("answer", "")))

    n_in, n_out = len(ins), len(outs)
    c_ok = count(ins, lambda x: x["verdict"] == "correct")
    c_ok_ru = sum(1 for a in ins if a["id"] in g and g[a["id"]]["verdict"] == "correct" and not cjk(a))
    c_part = count(ins, lambda x: x["verdict"] == "partial")
    c_bad = count(ins, lambda x: x["verdict"] == "incorrect")
    c_hall = count(ins + outs, lambda x: x.get("hallucination"))
    c_false_refusal = count(ins, lambda x: x.get("refused"))
    o_ok = count(outs, lambda x: x["verdict"] == "correct")
    o_ok_ru = sum(1 for a in outs if a["id"] in g and g[a["id"]]["verdict"] == "correct" and not cjk(a))
    c_cjk = sum(1 for a in ins + outs if cjk(a))
    c_stray = sum(1 for a in ins if stray_cites(a))
    # поля not_russian / stray_cite есть только во второй версии рубрики
    has_flags = any("stray_cite" in x for x in grades)
    j_stray = count(ins, lambda x: x.get("stray_cite"))
    m = summary["metrics"]
    lat = m["latency_s"]
    model_id = judge_model_id(judge_meta)
    asked = judge_meta.get("model")

    judge_metrics = {
        "judge_model": model_id,
        "in_correct": c_ok, "in_correct_russian": c_ok_ru,
        "in_partial": c_part, "in_incorrect": c_bad, "in_n": n_in,
        "out_refused_correctly": o_ok, "out_refused_russian": o_ok_ru, "out_n": n_out,
        "hallucinations": c_hall, "false_refusals": c_false_refusal, "cjk_answers": c_cjk,
        "stray_cites_script": c_stray,
    }
    if has_flags:
        judge_metrics["stray_cites_judge"] = j_stray
    judge_line = f"судья: `{model_id}`" + (f" (запрошен `{asked}`)" if asked and asked != model_id else "")
    lines = [
        f"# Прогон оценки: {os.path.basename(run_dir)}",
        "",
        f"Модель ассистента: `{summary['meta']['model'].get('weights', '?')}` · "
        f"{judge_line} · вопросов: {m['questions']} "
        f"({n_in} по корпусу, {n_out} вне корпуса), HTTP-ошибок: {m['http_errors']}",
        "",
        "## Метрики",
        "",
        "| Метрика | Значение | Как считается |",
        "|---|---|---|",
        f"| Верные ответы на русском | **{c_ok_ru}/{n_in} = {pct(c_ok_ru, n_in)}** | вердикт судьи correct и в ответе нет иероглифов (скрипт) |",
        f"| Верные ответы (судья) | {c_ok}/{n_in} = {pct(c_ok, n_in)} | вердикт correct по вопросам из корпуса |",
        f"| Частично верные | {c_part}/{n_in} = {pct(c_part, n_in)} | направление верное, пропущен шаг или неточность |",
        f"| Неверные | {c_bad}/{n_in} = {pct(c_bad, n_in)} | не те действия, противоречие корпусу или ложный отказ |",
        f"| Честный отказ вне корпуса на русском | **{o_ok_ru}/{n_out} = {pct(o_ok_ru, n_out)}** | вердикт судьи correct и нет иероглифов (скрипт) |",
        f"| Честный отказ вне корпуса (судья) | {o_ok}/{n_out} = {pct(o_ok, n_out)} | сказал «в runbook'ах нет», не выдумал |",
        f"| Ответы с выдумкой | {c_hall}/{n_in + n_out} = {pct(c_hall, n_in + n_out)} | команды или факты, которых нет в корпусе |",
        f"| Ложные отказы | {c_false_refusal}/{n_in} = {pct(c_false_refusal, n_in)} | «информации нет», хотя она есть |",
        f"| Ответы с иероглифами | {c_cjk}/{n_in + n_out} = {pct(c_cjk, n_in + n_out)} | модель сорвалась в китайский (считает скрипт) |",
        f"| Ссылка на посторонний runbook | {c_stray}/{n_in} = {pct(c_stray, n_in)} | скрипт: RB-NN в ответе не из ожидаемых для вопроса (верхняя оценка) |",
    ]
    if has_flags:
        lines.append(f"| Ссылка на runbook без единого шага из него (судья) | {j_stray}/{n_in} = {pct(j_stray, n_in)} | правило ссылок в рубрике |")
    lines += [
        f"| Нужный runbook в топ-8 | {pct(m['in_corpus']['hit@8'] * n_in, n_in)} | поиск: попал ли в контекст |",
        f"| Нужный runbook на 1-м месте | {pct(m['in_corpus']['hit@1'] * n_in, n_in)} | поиск: первый после реранка |",
        f"| Ссылается на нужный runbook | {pct(m['in_corpus']['cites_expected'] * n_in, n_in)} | RB-NN в тексте ответа |",
        f"| Покрытие ключевых фактов | {100 * m['in_corpus']['facts_coverage']:.0f}% | доля фактов из набора, найденных в ответе |",
        f"| Время ответа p50 / p95 | {lat['total']['p50']} / {lat['total']['p95']} с | полное время /ask |",
        f"| — из них поиск p50 | {lat['retrieval']['p50']} с | эмбеддинг + реранк на CPU |",
        f"| — из них генерация p50 | {lat['generation']['p50']} с | vLLM на GPU |",
        "",
        f"Балл реранка у лучшего фрагмента: по корпусу p50 {m['in_corpus']['top_rerank_score']['p50']} "
        f"(min {m['in_corpus']['top_rerank_score']['min']}), вне корпуса p50 "
        f"{m['out_of_corpus']['top_rerank_score']['p50']} (max {m['out_of_corpus']['top_rerank_score']['max']}).",
        "",
        "## Ответы с замечаниями",
        "",
        "Вердикт судьи не «верно» или скрипт нашёл иероглифы / ссылку на посторонний runbook.",
        "",
        "| id | вопрос | вердикт | замечания | комментарий судьи |",
        "|---|---|---|---|---|",
    ]
    for a in answers:
        x = g.get(a["id"])
        if not x:
            continue
        notes = (["выдумка"] if x.get("hallucination") else []) + \
                (["иероглифы"] if cjk(a) else []) + \
                ([f"лишняя ссылка {', '.join(stray_cites(a))}"] if stray_cites(a) else [])
        if x["verdict"] == "correct" and not notes:
            continue
        q = qmap[a["id"]]["question"].replace("|", "/")
        lines.append(f"| {a['id']} | {q[:90]} | {RU.get(x['verdict'], x['verdict'])} | "
                     f"{'; '.join(notes)} | {x.get('comment', '').replace('|', '/')} |")
    usage = judge_meta.get("usage") or {}
    mu = (usage.get("modelUsage") or {}).get(model_id) or {}
    tokens_in = sum(mu.get(k, 0) for k in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
    lines += ["", f"Судья `{model_id}`: {tokens_in:,} токенов на входе, {mu.get('outputTokens', 0):,} на выходе; "
                  f"стоимость по данным CLI ${usage.get('total_cost_usd')} "
                  f"(для подписки — эквивалент, не счёт), время {usage.get('duration_ms')} мс."]
    lines += list(extra)
    with open(os.path.join(run_dir, name), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return judge_metrics


def build_manual_sheet(run_dir, answers, questions, grades, n=10, seed=0):
    """Лист ручной проверки: n ответов (смесь вердиктов), кнопки «согласен / нет».
    Итог копируется одной кнопкой — так сверяется судья с человеком."""
    qmap = {q["id"]: q for q in questions}
    g = {x["id"]: x for x in grades}
    rnd = random.Random(seed)
    pool = [a for a in answers if a["id"] in g and a["status"] == 200]
    bad = [a for a in pool if g[a["id"]]["verdict"] != "correct"]
    outs = [a for a in pool if a["kind"] == "out" and g[a["id"]]["verdict"] == "correct"]
    good = [a for a in pool if a["kind"] == "in" and g[a["id"]]["verdict"] == "correct"]
    pick = rnd.sample(bad, min(4, len(bad))) + rnd.sample(outs, min(2, len(outs)))
    rest = [a for a in good if a not in pick]
    pick += rnd.sample(rest, min(n - len(pick), len(rest)))
    pick.sort(key=lambda a: a["id"])

    cards = []
    for a in pick:
        x, q = g[a["id"]], qmap[a["id"]]
        cards.append(f"""
<section class="card" data-id="{a['id']}" data-verdict="{x['verdict']}">
  <h2>{a['id']} · <span class="v {x['verdict']}">судья: {RU.get(x['verdict'], x['verdict'])}</span></h2>
  <p class="q">{html.escape(q['question'])}</p>
  <details open><summary>Эталон из набора</summary><p>{html.escape(q['reference'])}</p></details>
  <details open><summary>Ответ ассистента</summary><pre>{html.escape(a['answer'])}</pre></details>
  <p class="c">Комментарий судьи: {html.escape(x.get('comment') or '—')}</p>
  <div class="btns"><button data-a="agree">Согласен с судьёй</button><button data-a="disagree">Не согласен</button></div>
</section>""")
    page = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ручная проверка судьи</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1c1f24;--mute:#5d6470;--line:#dde1e7;--ok:#1f7a4d;--mid:#a86b00;--bad:#b3261e;--sel:#e8f0fe}}
@media (prefers-color-scheme:dark){{:root{{--bg:#15171b;--card:#1e2126;--ink:#e6e8eb;--mute:#9aa1ac;--line:#30343b;--sel:#233044}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:880px;margin:0 auto;padding:16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0}}
h1{{font-size:20px}} h2{{font-size:16px;margin:0 0 6px}} .q{{font-weight:600}}
pre{{white-space:pre-wrap;font:13px/1.45 ui-monospace,Consolas,monospace;background:var(--bg);padding:10px;border-radius:6px;overflow-x:auto}}
.v.correct{{color:var(--ok)}} .v.partial{{color:var(--mid)}} .v.incorrect{{color:var(--bad)}} .c{{color:var(--mute)}}
.btns button{{margin-right:8px;padding:6px 12px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--ink);cursor:pointer}}
.btns button.on{{background:var(--sel);border-color:#6b8fd6}}
#out{{width:100%;min-height:70px;font:13px ui-monospace,monospace}}
</style></head><body><main>
<h1>Ручная проверка судьи — {len(pick)} ответов</h1>
<p>Прочитай эталон и ответ, нажми «согласен» или «не согласен» с вердиктом судьи. Внизу — строка итога: скопируй её в чат.</p>
{''.join(cards)}
<p><button id="copy">Скопировать итог</button></p><textarea id="out" readonly></textarea>
</main><script>
const res = {{}};
function upd(){{
  const ids=[...document.querySelectorAll('.card')].map(c=>c.dataset.id);
  const done=ids.filter(i=>res[i]); const agree=done.filter(i=>res[i]==='agree').length;
  document.getElementById('out').value='Ручная проверка: согласен '+agree+'/'+done.length+' ('+ids.length+' в листе). '+
    done.map(i=>i+'='+(res[i]==='agree'?'да':'нет')).join(', ');
}}
document.querySelectorAll('.card').forEach(c=>c.querySelectorAll('button').forEach(b=>b.onclick=()=>{{
  res[c.dataset.id]=b.dataset.a; c.querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b)); upd();}}));
document.getElementById('copy').onclick=()=>{{const t=document.getElementById('out');t.select();navigator.clipboard&&navigator.clipboard.writeText(t.value);}};
upd();
</script></body></html>"""
    path = os.path.join(run_dir, "manual_check.html")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(page)
    return path


def archive_previous(run_dir, grades_path, meta_path):
    """Перед новым вызовом судьи прежние вердикты, отчёт и лист ручной проверки
    переименовываются с id прежней модели — чтобы сравнить судей на одних ответах."""
    if not os.path.exists(grades_path):
        return
    with open(meta_path, encoding="utf-8") as f:
        tag = judge_model_id(json.load(f))
    for name in ("grades.jsonl", "judge_meta.json", "report.md", "manual_check.html"):
        src = os.path.join(run_dir, name)
        if os.path.exists(src):
            stem, ext = os.path.splitext(name)
            os.replace(src, os.path.join(run_dir, f"{stem}_{tag}{ext}"))
    print(f"Прежние вердикты ({tag}) сохранены с суффиксом _{tag}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Судья: вердикты по ответам прогона + отчёт")
    ap.add_argument("run_dir")
    ap.add_argument("--model", default=DEFAULT_JUDGE, help="модель судьи для claude -p (точный id)")
    ap.add_argument("--claude-bin", default=None)
    ap.add_argument("--report-only", action="store_true", help="не звать судью, взять grades.jsonl")
    ap.add_argument("--second", action="store_true",
                    help="второй судья: вердикты в grades_<модель>.jsonl и report_<модель>.md со "
                         "сверкой с основным; grades.jsonl, report.md и summary.json не трогаются")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    answers = load_jsonl(os.path.join(run_dir, "answers.jsonl"))
    with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as f:
        summary = json.load(f)
    # набор — тот, на котором делался прогон (основной или отложенный)
    questions = load_jsonl(os.path.join(HERE, summary["meta"].get("questions_file", "questions.jsonl")))
    suffix = f"_{args.model}" if args.second else ""
    grades_path = os.path.join(run_dir, f"grades{suffix}.jsonl")
    meta_path = os.path.join(run_dir, f"judge_meta{suffix}.json")

    if args.report_only:
        grades = load_jsonl(grades_path)
        with open(meta_path, encoding="utf-8") as f:
            judge_meta = json.load(f)
    else:
        if not args.second:
            archive_previous(run_dir, grades_path, meta_path)
        qmap = {q["id"]: q for q in questions}
        items = [{"id": a["id"], "kind": a["kind"], "question": a["question"],
                  "reference": qmap[a["id"]]["reference"], "answer": a.get("answer", "")}
                 for a in answers if a["status"] == 200]
        prompt = (RUBRIC + "\n\n=== КОРПУС RUNBOOK'ОВ ===\n\n" + corpus_text() +
                  "\n\n=== ОТВЕТЫ НА ПРОВЕРКУ ===\n\n" + json.dumps(items, ensure_ascii=False, indent=1))
        print(f"Судья {args.model}: {len(items)} ответов, промпт {len(prompt):,} символов...", flush=True)
        grades, usage = call_judge(prompt, args.model, claude_bin(args.claude_bin))
        missing = {i["id"] for i in items} - {x["id"] for x in grades}
        if missing:
            print(f"ВНИМАНИЕ: судья пропустил {sorted(missing)}", flush=True)
        with open(grades_path, "w", encoding="utf-8", newline="\n") as f:
            for x in grades:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        judge_meta = {"model": args.model, "usage": usage}
        with open(meta_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(judge_meta, f, ensure_ascii=False, indent=2)

    if args.second:
        primary = load_jsonl(os.path.join(run_dir, "grades.jsonl"))
        with open(os.path.join(run_dir, "judge_meta.json"), encoding="utf-8") as f:
            primary_id = judge_model_id(json.load(f))
        extra = agreement_lines(answers, primary, grades, primary_id, judge_model_id(judge_meta))
        name = f"report_{args.model}.md"
        jm = build_report(run_dir, answers, questions, grades, summary, judge_meta, name, extra)
        print(json.dumps(jm, ensure_ascii=False, indent=2))
        print(f"\nОтчёт второго судьи: {os.path.join(run_dir, name)}")
        return

    jm = build_report(run_dir, answers, questions, grades, summary, judge_meta)
    summary["judge"] = jm
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    sheet = build_manual_sheet(run_dir, answers, questions, grades)
    print(json.dumps(jm, ensure_ascii=False, indent=2))
    print(f"\nОтчёт: {os.path.join(run_dir, 'report.md')}\nЛист ручной проверки: {sheet}")


if __name__ == "__main__":
    main()
