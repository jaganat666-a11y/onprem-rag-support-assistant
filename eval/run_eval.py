# -*- coding: utf-8 -*-
"""
Оценка качества RAG-ассистента на фиксированном наборе вопросов (eval/questions.jsonl).

Прогоняет каждый вопрос через живой фасад (POST /ask) и считает метрики, которым
судья не нужен:
  hit@1 / hit@8 — нужный runbook на первом месте / среди 8 фрагментов контекста;
  ссылка        — ответ называет нужный runbook (RB-NN в тексте);
  факты         — доля ключевых фактов из набора, найденных в ответе;
  отказ         — ответ похож на «в runbook'ах этого нет» (грубая эвристика;
                  вердикт ставит судья — eval/judge.py);
  время         — p50 / p95: полное время /ask, отдельно поиск и генерация.

Результат — папка eval/runs/<дата>_<метка>/: answers.jsonl (сырые ответы) и
summary.json (метрики). Следующий шаг — судья: py eval/judge.py <папка>.

Запуск (фасад :8080 и vLLM подняты):
  py eval/run_eval.py --tag qwen2.5-7b
  py eval/run_eval.py --limit 5            # быстрый прогон на 5 вопросах
  py eval/run_eval.py --kind out           # только 10 вопросов вне корпуса
Нужна только стандартная библиотека Python.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
RB_RX = re.compile(r"RB[-‐–\s]?0?(\d{1,2})", re.IGNORECASE)
# «Честный отказ»: модель говорит, что в фрагментах/runbook'ах ответа нет.
REFUSAL_RX = re.compile(
    r"(нет\s+(информации|данных|сведений|инструкци|описани)"
    r"|не\s+(описан|содерж|привод|привед|упомина|рассматрива|указан)"
    r"|отсутству"
    r"|(runbook|ранбук)\S*\s+(этого\s+)?нет)",
    re.IGNORECASE)


def rb_num(text):
    """'RB-09 — vLLM OOM ...' -> 9; None, если ID не найден."""
    m = RB_RX.search(text or "")
    return int(m.group(1)) if m else None


def post_json(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def served_model(vllm_base):
    """Какая модель сейчас в vLLM — пишем в метаданные прогона (для сравнения моделей)."""
    try:
        with urllib.request.urlopen(vllm_base.rstrip("/") + "/models", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))["data"][0]
        return {"served_name": data.get("id"), "weights": data.get("root"),
                "max_model_len": data.get("max_model_len")}
    except Exception as e:
        return {"error": str(e)}


def retrieval_config(facade):
    """Параметры поиска фасада (реранк, CAND_K, TOP_K) из /health — для сравнения прогонов."""
    try:
        with urllib.request.urlopen(facade.rstrip("/") + "/health", timeout=5) as r:
            return json.loads(r.read().decode("utf-8")).get("retrieval")
    except Exception as e:
        return {"error": str(e)}


def percentile(values, p):
    """Перцентиль с линейной интерполяцией (как numpy.percentile по умолчанию)."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def score_one(q, resp):
    """Метрики одного ответа без судьи."""
    answer = resp.get("answer", "")
    chunks = resp.get("chunks") or []
    top_nums = [rb_num(c["source"]) for c in chunks]
    expected = {rb_num(r) for r in q["runbooks"]}
    cited = {int(n) for n in RB_RX.findall(answer)}
    low = answer.casefold()
    found = [any(alt.casefold() in low for alt in group) for group in q["facts"]]
    row = {
        "top_ids": [f"RB-{n:02d}" if n else None for n in top_nums],
        "top_scores": [c["score"] for c in chunks],
        # разделы runbook'ов в контексте: при разборе провала видно, был ли у
        # модели нужный шаг (тогда виновата генерация) или его не нашёл поиск
        "top_sections": [c.get("section") for c in chunks],
        "cited": sorted(f"RB-{n:02d}" for n in cited),
        "refusal_heur": bool(REFUSAL_RX.search(answer)),
        "escalate": bool(resp.get("escalate")),   # поиск не уверен, модель не звали
    }
    if q["kind"] == "in":
        row.update(
            hit1=bool(top_nums) and top_nums[0] in expected,
            hit8=bool(expected & set(top_nums)),
            cite=bool(expected & cited),
            facts_found=found,
            facts_cov=(sum(found) / len(found)) if found else None,
        )
    return row


def summarize(rows):
    ok = [r for r in rows if r["status"] == 200]
    ins = [r for r in ok if r["kind"] == "in"]
    outs = [r for r in ok if r["kind"] == "out"]

    def share(xs, key):
        return round(sum(1 for x in xs if x[key]) / len(xs), 3) if xs else None

    def lat(key):
        vals = [r[key] for r in ok if r.get(key) is not None]
        return {"p50": round(percentile(vals, 50), 1), "p95": round(percentile(vals, 95), 1)} if vals else None

    def top_score(xs):
        vals = [r["top_scores"][0] for r in xs if r["top_scores"]]
        return {"min": min(vals), "p50": round(percentile(vals, 50), 3), "max": max(vals)} if vals else None

    return {
        "questions": len(rows),
        "http_errors": len(rows) - len(ok),
        "in_corpus": {
            "n": len(ins),
            "hit@1": share(ins, "hit1"),
            "hit@8": share(ins, "hit8"),
            "cites_expected": share(ins, "cite"),
            "facts_coverage": round(sum(r["facts_cov"] for r in ins) / len(ins), 3) if ins else None,
            "refusal_heur": share(ins, "refusal_heur"),
            "escalated": share(ins, "escalate"),
            "top_rerank_score": top_score(ins),
        },
        "out_of_corpus": {
            "n": len(outs),
            "refusal_heur": share(outs, "refusal_heur"),
            "escalated": share(outs, "escalate"),
            "top_rerank_score": top_score(outs),
        },
        "latency_s": {"total": lat("total_s"), "retrieval": lat("retrieval_s"), "generation": lat("gen_s")},
    }


def main():
    ap = argparse.ArgumentParser(description="Прогон набора вопросов через /ask и метрики без судьи")
    ap.add_argument("--facade", default="http://localhost:8080", help="адрес фасада")
    ap.add_argument("--vllm", default="http://localhost:8000/v1", help="адрес vLLM (только чтобы записать модель)")
    ap.add_argument("--questions", default=os.path.join(HERE, "questions.jsonl"))
    ap.add_argument("--tag", default="run", help="метка прогона в имени папки")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N вопросов")
    ap.add_argument("--kind", choices=("in", "out"), help="только вопросы по корпусу / вне корпуса")
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    with open(args.questions, encoding="utf-8") as f:
        questions = [json.loads(line) for line in f if line.strip()]
    if args.kind:
        questions = [q for q in questions if q["kind"] == args.kind]
    if args.limit:
        questions = questions[:args.limit]

    run_dir = os.path.join(HERE, "runs", datetime.now().strftime("%Y-%m-%d_%H%M") + "_" + args.tag)
    os.makedirs(run_dir, exist_ok=True)
    meta = {"started": datetime.now().isoformat(timespec="seconds"), "facade": args.facade,
            "model": served_model(args.vllm), "retrieval": retrieval_config(args.facade),
            "questions_file": os.path.basename(args.questions)}
    print(f"Прогон: {run_dir}\nМодель: {meta['model']}\nПоиск: {meta['retrieval']}\n"
          f"Вопросов: {len(questions)}\n", flush=True)

    rows = []
    with open(os.path.join(run_dir, "answers.jsonl"), "w", encoding="utf-8", newline="\n") as out:
        for i, q in enumerate(questions, 1):
            row = {k: q[k] for k in ("id", "kind", "runbooks", "question")}
            t0 = time.perf_counter()
            try:
                resp = post_json(args.facade.rstrip("/") + "/ask", {"question": q["question"]}, args.timeout)
                row.update(status=200, answer=resp["answer"], retrieval_s=resp.get("retrieval_s"),
                           gen_s=resp.get("latency_s"), completion_tokens=resp.get("completion_tokens"),
                           # runbook'и в контексте модели и строки кода, помеченные сторожем
                           sources=resp.get("sources"), unverified=resp.get("unverified"))
                row.update(score_one(q, resp))
            except urllib.error.HTTPError as e:
                resp = None
                row.update(status=e.code, error=e.read().decode("utf-8", "replace")[:300])
            except Exception as e:  # таймаут, фасад недоступен и т.п.
                resp = None
                row.update(status=0, error=str(e)[:300])
            row["total_s"] = round(time.perf_counter() - t0, 2)
            rows.append(row)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            mark = ("hit8" if row.get("hit8") else "MISS") if q["kind"] == "in" else \
                   ("отказ" if row.get("refusal_heur") else "ответил")
            print(f"[{i:2d}/{len(questions)}] {q['id']} {row['status']} {row['total_s']:5.1f} c  {mark}", flush=True)

    summary = {"meta": meta, "metrics": summarize(rows)}
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print("\n" + json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
    print(f"\nДальше — судья: py eval/judge.py {os.path.relpath(run_dir)}")


if __name__ == "__main__":
    main()
