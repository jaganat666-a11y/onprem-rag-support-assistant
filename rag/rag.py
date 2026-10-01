# -*- coding: utf-8 -*-
"""
RAG / Q&A по корпусу операционных runbook'ов поддержки.

Путь запроса: вопрос -> эмбеддинг bge-m3 (CPU) -> косинусный поиск top-CAND_K ->
реранк кросс-энкодером bge-reranker-v2-m3 (CPU) -> порог уверенности MIN_SCORE
(ниже — модель не зовём, подсказка ближайших runbook'ов) -> до CTX_RUNBOOKS
runbook'ов целиком в контекст -> генерация моделью qwen2.5-7b через vLLM
(OpenAI-совместимый API на :8000) -> сторож команд и строка «Источник» от кода.

Эмбеддер и реранкер качаются с HuggingFace ОДИН раз, дальше работают локально и
офлайн — в рантайме наружу ничего не уходит. Индекс кешируется на диск и
инвалидируется по content-hash: правка runbook'а пересчитывает только её чанки.

Происхождение: адаптировано из личного RAG-прототипа. Отличия здесь: источник —
корпус runbook'ов вместо личной вики, нарезка по секциям (а не по абзацам —
иначе рвутся блоки кода/SQL), генерация через vLLM вместо LM Studio.

Запуск (с Windows, vLLM поднят в WSL и слушает :8000):
  py rag/rag.py                 # батарея демо-вопросов + тайминги
  py rag/rag.py "свой вопрос"   # один вопрос
"""
import glob
import hashlib
import json
import os
import re
import sys
import time
import urllib.request

import numpy as np

# --- консоль в UTF-8 (Windows) ---
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# --- конфигурация ---
BASE = os.environ.get("VLLM_BASE", "http://localhost:8000/v1")  # vLLM, OpenAI-совместимый эндпоинт; в контейнере фасада переопределяется на http://vllm:8000/v1 (сервис compose)
CHAT_MODEL = "qwen2.5-7b"                   # served-model-name из команды запуска vLLM
ST_EMBED_MODEL = "BAAI/bge-m3"             # эмбеддинги: мультиязычный bi-encoder, CPU
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"   # реранк: кросс-энкодер, CPU
# Параметры поиска читаются из env, чтобы сравнивать варианты на одном наборе
# (eval/) без правки кода: RAG_RERANK=0 — без реранка, только косинус.
USE_RERANKER = os.environ.get("RAG_RERANK", "1") != "0"
# 12, а не 24: на наборе eval/ качество то же, поиск вдвое быстрее (docs/eval.md)
CAND_K = int(os.environ.get("RAG_CAND_K", "12"))  # кандидатов тянем эмбеддингом ДО реранка
TOP_K = int(os.environ.get("RAG_TOP_K", "8"))     # фрагментов ПОСЛЕ реранка: из них выбираются runbook'и
# Порог уверенности поиска: если у лучшего фрагмента балл реранка ниже, модель не
# зовём — отвечаем «уверенного ответа нет», подсказываем ближайшие runbook'и и
# помечаем вопрос для дежурного (escalate). На основном наборе eval/ по корпусу балл
# не ниже 0.455, вне корпуса не выше 0.112. Но на отложенном наборе коротких
# чатовых вопросов по корпусу балл падает до 0.02–0.2, а нужный runbook всё равно
# первый в 9 из 10 — поэтому ниже порога не глухой отказ, а подсказка (docs/eval.md).
# Работает только с реранком; 0 — выключен.
MIN_SCORE = float(os.environ.get("RAG_MIN_SCORE", "0.3"))
NO_ANSWER = "Уверенного ответа в runbook'ах нет."
SUGGEST_N = 3  # сколько ближайших runbook'ов подсказать ниже порога
# Контекст модели — целые runbook'и, а не TOP_K фрагментов вперемешку: до
# CTX_RUNBOOKS разных runbook'ов, чей лучший фрагмент не ниже MIN_SCORE, каждый
# целиком в порядке документа (Симптом → Диагностика → Решение → Эскалация).
# Фрагменты соседних runbook'ов с баллами 0.03–0.1 модель смешивала с нужным:
# диагностика не из того runbook'а, шаги без предупреждений (docs/eval.md).
# Два самых длинных runbook'а — ~3.2К токенов, с запасом в max_model_len 8192.
# 0 — прежний режим: TOP_K фрагментов.
CTX_RUNBOOKS = int(os.environ.get("RAG_CTX_RUNBOOKS", "2"))
CTX_SKIP = {"Связано"}  # ссылки на другие runbook'и: шагов нет, только уводят модель

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # корень репозитория
CORPUS_GLOB = os.path.join(ROOT, "corpus", "runbooks", "*.md")
# диск-кеш эмбеддингов рядом со скриптом (gitignored, regenerated from corpus)
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_index_cache")


def post(path, payload, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# --- 1. читаем корпус, режем на чанки ПО СЕКЦИЯМ (## ...) ---
def parse_frontmatter(raw):
    """Отделяем YAML-фронтматтер от тела. Возвращаем (meta: dict, body: str)."""
    meta = {}
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            return meta, parts[2]
    return meta, raw


def load_chunks():
    """Каждый runbook режем по заголовкам уровня 2 (## Симптом, ## Диагностика, ...).
    Чанк = одна секция: семантически цельная и не рвёт блоки кода/SQL/LogQL внутри.
    Возвращаем list[(source, header, text)], где source = 'RB-NN — заголовок'."""
    chunks = []
    for fp in sorted(glob.glob(CORPUS_GLOB)):
        with open(fp, encoding="utf-8") as f:
            meta, body = parse_frontmatter(f.read())
        rid = meta.get("id", os.path.basename(fp))
        title = meta.get("title", "")
        source = f"{rid} — {title}".strip(" —")
        # режем тело перед каждым '## '; кусок до первого '## ' (блок H1) отбрасываем
        for sec in re.split(r"\n(?=## )", body):
            sec = sec.strip()
            if not sec:
                continue
            lines = sec.splitlines()
            if lines[0].startswith("## "):
                header = lines[0].lstrip("# ").strip()
                text = "\n".join(lines[1:]).strip()
            else:
                continue  # H1-заголовок/преамбула — он уже учтён в source
            if len(text) < 30:
                continue
            chunks.append((source, header, text))
    return chunks


def chunk_doc(c):
    """Текст, который реально эмбеддим/реранкаем: ярлык источника + секция + тело.
    Префикс источника помогает поиску различать одинаковые секции разных runbook'ов."""
    source, header, text = c
    return f"{source} · {header}\n{text}"


# --- 2. эмбеддинги bge-m3 (sentence-transformers, CPU, мультиязычный) ---
_st_embedder = None


def get_st_embedder():
    global _st_embedder
    if _st_embedder is None:
        from sentence_transformers import SentenceTransformer
        _st_embedder = SentenceTransformer(ST_EMBED_MODEL, device="cpu")
    return _st_embedder


def embed(texts):
    """bge-m3 мультиязычный, query/doc-префиксы не нужны. Нормируем -> косинус = dot."""
    m = get_st_embedder()
    # show_progress_bar=False: tqdm пишет НЕ-JSON в stderr и засоряет поток фасада,
    # который забирает Promtail -> Loki (нужен единый JSON-формат; см. facade/app.py).
    vecs = m.encode(texts, batch_size=16, normalize_embeddings=True, show_progress_bar=False)
    return np.asarray(vecs, dtype=np.float32)


# --- 2a. персистентный индекс: кеш эмбеддингов, пересчёт только изменённых чанков ---
def chunk_hash(text):
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()


def build_index(chunks, verbose=True):
    """Эмбеддинги всех чанков с диск-кешем. Пересчитываем ТОЛЬКО чанки, чей content-hash
    изменился; неизменные берём из npy. Кеш привязан к модели эмбеддера — смена модели
    сбрасывает его. Битый/несовместимый кеш -> честный пересчёт. Возвращает матрицу (N x D)."""
    texts = [chunk_doc(c) for c in chunks]
    hashes = [chunk_hash(t) for t in texts]
    npy_path = os.path.join(CACHE_DIR, "embeds.npy")
    man_path = os.path.join(CACHE_DIR, "manifest.json")

    cache = {}  # hash -> вектор (np.float32)
    if os.path.exists(npy_path) and os.path.exists(man_path):
        try:
            with open(man_path, encoding="utf-8") as f:
                man = json.load(f)
            if man.get("model") == ST_EMBED_MODEL:
                mat = np.load(npy_path)
                if mat.shape[0] == len(man.get("hashes", [])):
                    cache = {h: mat[i] for i, h in enumerate(man["hashes"])}
        except Exception:
            cache = {}  # битый/несовместимый кеш -> пересчёт с нуля

    missing = [i for i, h in enumerate(hashes) if h not in cache]
    if missing:
        fresh = embed([texts[i] for i in missing])
        for i, v in zip(missing, fresh):
            cache[hashes[i]] = v

    mat = np.vstack([cache[h] for h in hashes]).astype(np.float32)
    os.makedirs(CACHE_DIR, exist_ok=True)
    np.save(npy_path, mat)
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump({"model": ST_EMBED_MODEL, "dim": int(mat.shape[1]), "hashes": hashes}, f)
    if verbose:
        print(f"индекс: {len(chunks)} чанков (из кеша {len(chunks) - len(missing)}, "
              f"пересчитано {len(missing)})", flush=True)
    return mat


# --- 2b. реранкер (кросс-энкодер, CPU): чинит промахи bi-encoder-поиска ---
_reranker = None


def get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        _reranker = CrossEncoder(RERANK_MODEL, device="cpu", max_length=512)
    return _reranker


def retrieve(question, chunks, mat):
    """CAND_K кандидатов по косинусу -> (опц.) реранк -> top-K. Возвращает ([(score, i)], метод)."""
    qvec = embed([question])[0]
    sims = mat @ qvec                       # нормированные векторы: dot = cosine
    cand_idx = np.argsort(-sims)[:CAND_K]
    cand = [(float(sims[i]), int(i)) for i in cand_idx]
    if not USE_RERANKER:
        return cand[:TOP_K], "cosine"
    ce = get_reranker()
    pairs = [[question, chunk_doc(chunks[i])] for _, i in cand]
    rs = ce.predict(pairs, show_progress_bar=False)  # без tqdm — единый JSON-поток для Loki
    ranked = sorted(((float(s), i) for s, (_, i) in zip(rs, cand)), reverse=True)[:TOP_K]
    return ranked, "rerank"


# --- 3. ответ qwen2.5-7b по найденному контексту (через vLLM) ---
SYS = ("Ты — ассистент дежурного инженера поддержки. Отвечай по делу, "
       "опираясь ТОЛЬКО на приведённые фрагменты runbook'ов. Перечисли все шаги и команды "
       "из раздела «Решение» подходящего runbook'а, не пропуская ни одного. "
       "Если ответа во фрагментах нет — "
       "честно скажи, что в runbook'ах этого нет, и не выдумывай. Команды, SQL и LogQL "
       "приводи точно как в источнике; не добавляй команд, которых во фрагментах нет. "
       "Пиши ИСКЛЮЧИТЕЛЬНО на русском языке: латиница допустима только внутри команд, "
       "путей, имён полей и идентификаторов. Не используй иероглифы и другие алфавиты. "
       "Не перечисляй источники в конце ответа — их добавит система.")


def label(source):
    """Ярлык runbook'а для вывода: без кавычек из YAML-заголовка. Сам source не
    трогаем — он входит в текст, по которому считаются эмбеддинги и баллы."""
    return source.replace('"', "")


def no_answer(hits):
    """Отказ с подсказкой: до SUGGEST_N разных runbook'ов в порядке реранка.
    Модель не вызывается — выдумке и срыву в китайский взяться неоткуда."""
    near = list(dict.fromkeys(h["source"] for h in hits))[:SUGGEST_N]
    lines = [NO_ANSWER]
    if near:
        lines += ["Ближе всего по поиску — проверьте, подходит ли:"] + [f"- {label(s)}" for s in near]
    lines.append("Вопрос передан дежурному инженеру.")
    return "\n".join(lines)


def context_runbooks(scored, chunks):
    """Какие runbook'и целиком уходят в контекст: найденный первым — всегда (порог
    он уже прошёл), следующие — если их лучший фрагмент не ниже MIN_SCORE."""
    picked = []
    for s, i in scored:  # по убыванию балла
        source = chunks[i][0]
        if source in picked:
            continue
        if picked and s < MIN_SCORE:
            break
        picked.append(source)
        if len(picked) == CTX_RUNBOOKS:
            break
    return picked


def build_context(scored, chunks):
    """Текст контекста для модели и список runbook'ов в нём."""
    if CTX_RUNBOOKS <= 0:  # прежний режим: фрагменты в порядке реранка
        context = "\n\n".join(f"[{chunks[i][0]} · {chunks[i][1]}]\n{chunks[i][2]}"
                              for _, i in scored)
        return context, sorted({chunks[i][0] for _, i in scored})
    picked = context_runbooks(scored, chunks)
    # load_chunks идёт по файлу сверху вниз — секции runbook'а уже в порядке документа
    context = "\n\n".join(
        f"[{source}]\n" + "\n\n".join(f"## {h}\n{t}" for s, h, t in chunks
                                      if s == source and h not in CTX_SKIP)
        for source in picked)
    return context, picked


# --- 4. сторож команд: строки команд, которых нет в контексте модели ---
# Ловит добавленные и изменённые строки; пропущенные шаги и предупреждения не ловит.
# Строка блока кода, которая лишь часть команды runbook'а (без флага или условия), —
# обрезанная команда, если целиком команда в этом блоке не собирается (модель вправе
# разбить длинный SQL на строки). Последний блок без закрывающих ``` — ответ обрезан
# по max_tokens, проверяем и его.
FENCE_RX = re.compile(r"```[^\n]*\n(.*?)(```|\Z)", re.S)
INLINE_RX = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
# инлайн-спан — команда, если начинается с команды из корпуса: `docker logs …`, но не
# `cnt > 1` и не `Payments / Webhook Health` (цитаты и названия панелей)
INLINE_CMD_RX = re.compile(r"^(docker|curl|git|psql|redis-cli|kubectl|sed|df|ss|nvidia-smi|"
                           r"truncate|php|systemctl|journalctl|wsl|SELECT|UPDATE|INSERT|"
                           r"DELETE|ALTER|DROP|TRUNCATE)\s+\S")
COMMENT_RX = re.compile(r"^(#|--|//)")
PROSE_RX = re.compile(r"^[А-Яа-яЁё]")       # обычный текст, положенный в блок кода
TAIL_COMMENT_RX = re.compile(r"\s+#\s.*$")  # модель переводит комментарии на русский
PLACEHOLDER_RX = re.compile(r"<[\w][\w .-]*>")  # <user_id>, <ваша ветка>
VALUE_RX = r"[\w.-]+"                          # значение, вместо которого бывает подстановка
UNVERIFIED = "⚠️ Этих строк нет в runbook'ах — не выполнять без проверки:"
NOTE_RX = re.compile(r"\n" + re.escape(UNVERIFIED) + r"(?:\n- `[^\n]*`)+")


def norm(s):
    return " ".join(s.split())


def block_lines(block):
    """Строки команд блока кода: пробелы схлопнуты; комментарии, пустые строки
    и строки с русского слова не в счёт."""
    for line in block.splitlines():
        line = norm(line).rstrip("\\").strip()
        if line and not COMMENT_RX.match(line) and not PROSE_RX.match(line):
            yield line


def code_lines(text):
    """Строки команд из всех блоков кода текста."""
    for m in FENCE_RX.finditer(text or ""):
        yield from block_lines(m.group(1))


def inline_commands(text):
    """Команды в `инлайн`-спанах вне блоков кода (с латинского слова и с пробелом);
    списки ⚠️ самого сторожа не в счёт. Без повторов, в порядке появления."""
    prose = NOTE_RX.sub("", FENCE_RX.sub("", text or ""))
    spans = (norm(s) for s in INLINE_RX.findall(prose))
    return list(dict.fromkeys(s for s in spans if INLINE_CMD_RX.match(s)))


def command_lines(text):
    """Что сторож сверяет в ответе: (строка, текст её блока) для строк блоков кода
    и (команда, None) для инлайн-команд."""
    for m in FENCE_RX.finditer(text or ""):
        block = norm(m.group(1))
        for line in block_lines(m.group(1)):
            yield line, block
    for span in inline_commands(text):
        yield span, None


def reference(text):
    """Эталон: (целые строки команд runbook'ов — из блоков кода, и без хвоста ` # …`,
    и инлайн-спаны; весь текст одной строкой — для цитат из пояснений и комментариев)."""
    cmds = {norm(s) for s in INLINE_RX.findall(FENCE_RX.sub("", text or ""))}
    for line in code_lines(text):
        cmds |= {line, TAIL_COMMENT_RX.sub("", line)}
    return cmds, norm(text or "")


def _pattern(line):
    """Строка как регулярка: подстановка совпадает с конкретным значением из примера
    (<user_id> там, где в runbook'е 10042) или с той же подстановкой, но не с другой
    (<blocked_pid> ≠ <blocking_pid>). Остальная строка — дословно."""
    names = PLACEHOLDER_RX.findall(line)
    parts = [re.escape(p) for p in PLACEHOLDER_RX.split(line)]
    return re.compile(parts[0] + "".join(f"(?:{re.escape(nm)}|{VALUE_RX}){p}"
                                         for nm, p in zip(names, parts[1:])))


def line_in(line, ref, block=None):
    """Есть ли строка в эталоне; хвост ` # …` не в счёт. Строка блока кода (block —
    его текст) — целая строка команды или цитата из текста runbook'а, но не часть
    команды, если целиком та в блоке не собрана. Инлайн-команда (block=None) — цитата."""
    cmds, flat = ref
    for cand in dict.fromkeys((line, TAIL_COMMENT_RX.sub("", line))):
        rx = _pattern(cand)
        if block is not None:
            if any(rx.fullmatch(k) for k in cmds):
                return True
            parts_of = [k for k in cmds if rx.search(k)]
            if parts_of:  # обрезанная команда — или длинная, разбитая на строки
                if any(k in block for k in parts_of):
                    return True
                continue
        if rx.search(flat):
            return True
    return False


def mark_unverified(text, context):
    """Под блоком кода перечисляем его строки, которых нет в контексте модели;
    инлайн-команды не из контекста — общим списком в конце ответа.
    Запрет в промпте 7B-модель нарушает: выдуманные INSERT/ALTER в 4 из 40 ответов
    (docs/eval.md). Не удаляем — дежурный видит команду и предупреждение рядом.
    Возвращает (текст, число помеченных строк)."""
    ref = reference(context)
    n = 0

    def note(m):
        nonlocal n
        block = m.group(0) if m.group(2) else m.group(0).rstrip() + "\n```"  # обрезан: закрываем
        bad = [x for x, b in command_lines(block) if not line_in(x, ref, b)]
        n += len(bad)
        if not bad:
            return block
        return block + "\n" + UNVERIFIED + "\n" + "\n".join(f"- `{x}`" for x in bad)

    text = FENCE_RX.sub(note, text)
    bad = [x for x in inline_commands(text) if not line_in(x, ref)]
    if bad:
        n += len(bad)
        text += "\n\n" + UNVERIFIED + "\n" + "\n".join(f"- `{x}`" for x in bad)
    return text, n


def answer(question, chunks, mat):
    """Полный путь: поиск -> генерация. Возвращает dict: ответ, источники, ранжированные
    фрагменты контекста (hits) и время каждого этапа — поиск (CPU) и генерация (vLLM)."""
    t_ret = time.perf_counter()
    scored, how = retrieve(question, chunks, mat)
    retrieval_s = time.perf_counter() - t_ret
    # hits — фрагменты в порядке после реранка: по ним оценка считает, на каком месте
    # оказался нужный runbook (hit@1 / hit@8), а балл лучшего — уверенность поиска.
    hits = [{"source": chunks[i][0], "section": chunks[i][1], "score": round(s, 3)}
            for s, i in scored]
    if how == "rerank" and MIN_SCORE > 0 and (not scored or scored[0][0] < MIN_SCORE):
        # поиск не уверен -> модель не зовём: вне корпуса она выдумывает и срывается
        # в китайский (разбор провалов в docs/eval.md); отказ с подсказкой + эскалация
        return {"answer": no_answer(hits), "sources": [], "hits": hits, "retrieval": how,
                "retrieval_s": retrieval_s, "gen_s": 0.0, "completion_tokens": 0,
                "unverified": 0, "escalate": True}
    context, sources = build_context(scored, chunks)  # sources — что ушло в контекст (журнал)
    msg = [{"role": "system", "content": SYS},
           {"role": "user", "content": f"Фрагменты runbook'ов:\n\n{context}\n\n---\nВопрос: {question}"}]
    t0 = time.perf_counter()
    resp = post("/chat/completions", {
        "model": CHAT_MODEL, "messages": msg,
        # температура 0 — жадная генерация: при 0.2 вердикты по одним и тем же вопросам
        # перемешивались между прогонами (docs/eval.md), замер не отличал правку от шума.
        # qwen2.5-7b-Instruct — не reasoning-модель, thinking-параметры не нужны.
        "temperature": 0, "max_tokens": 1024,
    })
    dt = time.perf_counter() - t0
    if not resp.get("choices"):
        raise ValueError(f"ответ vLLM без choices: {str(resp)[:200]}")
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    ctoks = resp.get("usage", {}).get("completion_tokens", 0)
    text, unverified = mark_unverified(text, context)
    # Ссылку ставит код, а не модель: попросишь 7B-модель назвать источники — она
    # перечисляет всё, что было в контексте (лишние ссылки в 10/40 ответов при любой
    # формулировке промпта, docs/eval.md). Называем runbook, найденный поиском первым.
    cited = [chunks[scored[0][1]][0]] if scored else []
    if cited:
        text += f"\n\nИсточник: {label(cited[0])}"
    return {"answer": text, "sources": sources, "cited": cited, "hits": hits,
            "retrieval": how, "retrieval_s": retrieval_s, "gen_s": dt,
            "completion_tokens": ctoks, "unverified": unverified, "escalate": False}


DEFAULT_QS = [
    "Пользователь сообщает: пополнение по СБП прошло, деньги списаны в банке, но баланс карты не изменился. Что делать?",
    "Как проверить идемпотентность платёжного вебхука и не допустить двойного зачисления?",
    "Контейнер vLLM упал с OOM на 8-ГБ карте — как это диагностировать и какой временный workaround применить?",
    "Входящие вебхуки эквайера отклоняются с HTTP 401 после ротации секрета. Как найти причину и починить?",
    "Опиши порядок эскалации L1 -> L2 -> L3 при инциденте с платёжным вебхуком.",
]


def main():
    print("Индексирую корпус runbook'ов...", flush=True)
    t0 = time.perf_counter()
    chunks = load_chunks()
    if not chunks:
        print(f"Корпус пуст: ничего не найдено по {CORPUS_GLOB}", flush=True)
        return
    mat = build_index(chunks)
    idx_dt = time.perf_counter() - t0
    print(f"Чанков: {len(chunks)} | индексация: {idx_dt:.1f} c\n", flush=True)

    if USE_RERANKER:
        print(f"Гружу реранкер {RERANK_MODEL} (CPU)...", flush=True)
        tr = time.perf_counter()
        get_reranker()
        print(f"Реранкер готов: {time.perf_counter() - tr:.1f} c\n", flush=True)

    qs = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else DEFAULT_QS
    speeds = []
    for q in qs:
        r = answer(q, chunks, mat)
        dt, ctoks = r["gen_s"], r["completion_tokens"]
        tps = (ctoks / dt) if dt > 0 and ctoks else 0
        if tps:
            speeds.append(tps)
        print("=" * 70)
        print("В:", q)
        print("О:", r["answer"])
        print(f"[поиск {r['retrieval_s']:.1f} c | генерация {dt:.1f} c, {ctoks} ток, {tps:.1f} ток/с | "
              f"источники: {', '.join(r['sources'])}]\n", flush=True)
    if speeds:
        print(f"\nСредняя скорость генерации: {sum(speeds) / len(speeds):.1f} ток/с "
              f"({len(speeds)} замеров).")


if __name__ == "__main__":
    main()
