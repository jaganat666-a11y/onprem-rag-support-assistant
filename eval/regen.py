# -*- coding: utf-8 -*-
"""
Тот же поиск и тот же контекст — ответ пишет другая модель. Отделяет вклад модели
от вклада конвейера: если сильная модель на том же контексте отвечает верно,
провалы локальной 7B — цена модели, а не поиска и не сборки контекста.

Берёт готовый прогон run_eval.py и по найденным фрагментам из answers.jsonl
восстанавливает контекст (rag.build_context строится из корпуса, без моделей и
без GPU). Дальше всё как в фасаде: тот же системный промпт rag.SYS, тот же сторож
команд, та же строка «Источник». Вопросы ниже порога модель не видит и в
локальном прогоне — они переносятся как есть. Результат — новая папка прогона в
том же формате: дальше judge.py и check_commands.py как обычно.

Генератор:
  claude-<модель>   — Claude через Claude Code CLI (`claude -p`); системный промпт
                      CLI заменён на rag.SYS, инструментов и MCP нет.
  GigaChat-<модель> — GigaChat API Сбера (облако). Ключ авторизации — в env
                      GIGACHAT_AUTH_KEY или строкой в .env в корне репозитория
                      (.env в git не попадает). Серверы GigaChat подписаны
                      корневым сертификатом Минцифры — он лежит в eval/certs/ и
                      передаётся только в эти соединения, в систему не ставится.
                      `--gen GigaChat` без модели — список доступных моделей.

Почему внешняя модель допустима: корпус и вопросы синтетические.

Запуск:
  py eval/regen.py eval/runs/2026-10-01_1153_whole_rb --gen claude-sonnet-5-5
  py eval/regen.py eval/runs/2026-10-01_1153_whole_rb --gen GigaChat-2-Max
"""
import argparse
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # sys.exit и трассировки — тоже по-русски
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
from judge import claude_bin, load_jsonl  # noqa: E402 — после sys.path
from rag import rag  # noqa: E402
from run_eval import score_one, summarize  # noqa: E402


def gen_claude(model, binary):
    def gen(system, user):
        cmd = [binary, "-p", "--model", model, "--output-format", "json",
               "--system-prompt", system, "--tools", "", "--strict-mcp-config",
               "--no-session-persistence", "--safe-mode"]
        # как в judge.py: без служебной мелкой модели и без чужих CLAUDE.md
        env = dict(os.environ, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8",
                              cwd=tempfile.gettempdir(), env=env, timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p: код {proc.returncode}: {proc.stderr[-500:]}")
        env_ = json.loads(proc.stdout)
        mu = env_.get("modelUsage") or {}
        return (env_.get("result") or "").strip(), {
            "cost_usd": env_.get("total_cost_usd") or 0,
            "out_tokens": sum(v.get("outputTokens", 0) for v in mu.values()),
            "in_tokens": sum(v.get("inputTokens", 0) + v.get("cacheReadInputTokens", 0)
                             + v.get("cacheCreationInputTokens", 0) for v in mu.values()),
            "models": sorted(mu)}
    return gen


GIGA_OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGA_API = "https://gigachat.devices.sberbank.ru/api/v1"
GIGA_CA = os.path.join(HERE, "certs", "russian_trusted_root_ca.cer")


def env_value(name):
    """Переменная окружения или строка NAME=... из .env в корне репозитория."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k.strip() == name:
                    return v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return None


class GigaChat:
    """Клиент GigaChat API: ключ авторизации -> токен доступа (живёт 30 мин) -> запросы."""

    def __init__(self):
        self.key = env_value("GIGACHAT_AUTH_KEY")
        if not self.key:
            sys.exit("Нет GIGACHAT_AUTH_KEY: задайте в env или строкой в .env в корне репозитория")
        self.scope = env_value("GIGACHAT_SCOPE") or "GIGACHAT_API_PERS"  # физлицо
        self.ctx = ssl.create_default_context(cafile=GIGA_CA)
        self.lock = threading.Lock()
        self.token, self.expires = None, 0.0

    def _token(self):
        with self.lock:
            if time.time() > self.expires - 60:
                req = urllib.request.Request(
                    GIGA_OAUTH, data=urllib.parse.urlencode({"scope": self.scope}).encode(),
                    headers={"Authorization": f"Basic {self.key}", "RqUID": str(uuid.uuid4()),
                             "Content-Type": "application/x-www-form-urlencoded",
                             "Accept": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=30, context=self.ctx) as r:
                        d = json.loads(r.read())
                except urllib.error.HTTPError as e:
                    raise RuntimeError(f"GigaChat: токен не выдан, HTTP {e.code}: "
                                       f"{e.read().decode('utf-8', 'replace')[:300]}")
                self.token, self.expires = d["access_token"], d["expires_at"] / 1000  # мс
            return self.token

    def call(self, path, body=None):
        req = urllib.request.Request(
            f"{GIGA_API}{path}", data=json.dumps(body).encode() if body else None,
            headers={"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300, context=self.ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GigaChat {path}: HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def gen_gigachat(model):
    api = GigaChat()

    def gen(system, user):
        # температура по умолчанию API: claude -p свою тоже не задаёт
        d = api.call("/chat/completions", {"model": model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}]})
        u = d.get("usage") or {}
        return d["choices"][0]["message"]["content"].strip(), {
            "cost_usd": 0, "in_tokens": u.get("prompt_tokens", 0),
            "out_tokens": u.get("completion_tokens", 0), "models": [d.get("model", model)]}
    return gen


def scored_from_row(row, index):
    """Найденные фрагменты прогона -> [(балл, индекс чанка)], как у rag.retrieve."""
    return [(s, index[(rb, sec)])
            for rb, s, sec in zip(row["top_ids"], row["top_scores"], row["top_sections"])]


def main():
    ap = argparse.ArgumentParser(description="Перегенерировать ответы прогона другой моделью")
    ap.add_argument("run_dir")
    ap.add_argument("--gen", required=True,
                    help="claude-<модель> (claude-sonnet-5-5) или GigaChat-<модель> (GigaChat-2-Max)")
    ap.add_argument("--workers", type=int, default=None,
                    help="параллельных вызовов: Claude — 4, GigaChat — 1 (лимит потоков у физлица)")
    ap.add_argument("--claude-bin", default=None)
    ap.add_argument("--ids", default="", help="только эти вопросы через запятую (проба)")
    args = ap.parse_args()
    src = os.path.abspath(args.run_dir)
    if args.gen.startswith("claude-"):
        gen, via, workers = gen_claude(args.gen, claude_bin(args.claude_bin)), "claude -p", 4
    elif args.gen == "GigaChat":
        for m in GigaChat().call("/models")["data"]:
            print(m["id"])
        return
    elif args.gen.startswith("GigaChat-"):
        gen, via, workers = gen_gigachat(args.gen), "GigaChat API", 1
    elif args.gen == "guard":
        # модель не зовём: ответы исходного прогона + пометки сторожа — чтобы прогон,
        # снятый до сторожа, судился в тех же условиях, что и новые
        gen, via, workers = None, "guard", 1
    else:
        sys.exit("--gen: claude-<модель>, GigaChat-<модель> или guard")
    workers = args.workers or workers

    with open(os.path.join(src, "summary.json"), encoding="utf-8") as f:
        meta = json.load(f)["meta"]
    # контекст — с теми же параметрами, что были у фасада в исходном прогоне
    ret = meta.get("retrieval") or {}
    rag.MIN_SCORE = ret.get("min_score", rag.MIN_SCORE)
    rag.CTX_RUNBOOKS = ret.get("ctx_runbooks", 0)  # прогоны до 2026-10-01 — фрагменты
    questions = {q["id"]: q for q in load_jsonl(os.path.join(HERE, meta.get("questions_file", "questions.jsonl")))}
    rows = load_jsonl(os.path.join(src, "answers.jsonl"))
    if args.ids:
        rows = [r for r in rows if r["id"] in args.ids.split(",")]

    chunks = rag.load_chunks()
    index = {(source.split(" ")[0], header): i for i, (source, header, _) in enumerate(chunks)}

    def one(row):
        if row["status"] != 200 or row.get("escalate"):
            return row, None  # модель не звали и в исходном прогоне
        scored = scored_from_row(row, index)
        context, sources = rag.build_context(scored, chunks)
        if row.get("sources") and row["sources"] != sources:
            raise RuntimeError(f"{row['id']}: контекст не совпал с исходным прогоном")
        user = f"Фрагменты runbook'ов:\n\n{context}\n\n---\nВопрос: {row['question']}"
        out = {k: row[k] for k in ("id", "kind", "runbooks", "question")}
        t0 = time.perf_counter()
        try:
            if gen is None:  # guard: прежний текст без строки «Источник» (её добавим ниже)
                text = row["answer"].rsplit("\n\nИсточник: ", 1)[0]
                usage = {"cost_usd": 0, "in_tokens": 0, "models": [],
                         "out_tokens": row.get("completion_tokens") or 0}
            else:
                text, usage = gen(rag.SYS, user)
        except Exception as e:  # кончился лимит, сеть и т.п. — остальные ответы не теряем
            print(f"{row['id']}: {e}", flush=True)
            out.update(status=0, error=str(e)[:300])
            return out, None
        gen_s = row.get("gen_s") or 0 if gen is None else time.perf_counter() - t0
        text, unverified = rag.mark_unverified(text, context)
        text += f"\n\nИсточник: {rag.label(chunks[scored[0][1]][0])}"
        resp = {"answer": text, "escalate": False,
                "chunks": [{"source": rb, "score": s, "section": sec} for rb, s, sec in
                           zip(row["top_ids"], row["top_scores"], row["top_sections"])]}
        out.update(status=200, answer=text, retrieval_s=row.get("retrieval_s"),
                   gen_s=round(gen_s, 2), completion_tokens=usage["out_tokens"],
                   sources=sources, unverified=unverified)
        out.update(score_one(questions[row["id"]], resp))
        out["total_s"] = round((row.get("retrieval_s") or 0) + gen_s, 2)
        return out, usage

    t0 = time.time()
    with ThreadPoolExecutor(workers) as ex:
        results = list(ex.map(one, rows))
    new_rows = [r for r, _ in results]
    usages = [u for _, u in results if u]

    stamp = time.strftime("%Y-%m-%d_%H%M")
    dst = os.path.join(HERE, "runs", f"{stamp}_regen_{args.gen}")
    os.makedirs(dst, exist_ok=True)
    with open(os.path.join(dst, "answers.jsonl"), "w", encoding="utf-8", newline="\n") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    cost = {"calls": len(usages), "cost_usd": round(sum(u["cost_usd"] for u in usages), 3),
            "in_tokens": sum(u["in_tokens"] for u in usages),
            "out_tokens": sum(u["out_tokens"] for u in usages),
            "models": sorted({m for u in usages for m in u["models"]})}
    new_meta = dict(meta, started=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    model=meta["model"] if gen is None else
                    {"served_name": args.gen, "weights": f"{args.gen} ({via})"},
                    regen_from=os.path.basename(src), generator_usage=cost)
    with open(os.path.join(dst, "summary.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"meta": new_meta, "metrics": summarize(new_rows)}, f, ensure_ascii=False, indent=2)
    print(f"{len(usages)} вызовов за {time.time() - t0:.0f} c; {json.dumps(cost, ensure_ascii=False)}")
    failed = [r["id"] for r in new_rows if r["status"] != 200]
    if failed:
        print(f"Без ответа ({len(failed)}): {', '.join(failed)} — судья их пропустит")
    print(f"Прогон: {dst}")


if __name__ == "__main__":
    main()
