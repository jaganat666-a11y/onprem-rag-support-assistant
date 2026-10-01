# Setup layers: launch and internals

[Русский](operations.md) · English

How to bring up each layer and what is inside it. The environment is WSL2
(Ubuntu) and Docker; only the model server needs a GPU, everything else runs on
the CPU.

## Launch

The whole setup in one command. The first run builds the facade image and
downloads the models (Qwen ~5 GB for vLLM, bge-m3 and the reranker for the
facade) — a few minutes.

```bash
docker compose --profile gpu up -d   # vLLM :8000, facade :8080, PostgreSQL :5432,
                                     # Grafana :3000 (admin / see .env.example),
                                     # Prometheus :9090, Loki :3100
```

Services that need an NVIDIA GPU (`vllm`, `gpu-exporter`) are in the `gpu`
profile. Without the profile only the CPU part comes up — so the setup also
runs on a machine without a GPU, and the facade responds 503 "degraded":

```bash
docker compose up -d                                   # everything except vllm and gpu-exporter
docker compose up -d prometheus grafana node-exporter  # monitoring only, without building the facade
```

Grafana dashboards are picked up automatically from
`monitoring/grafana/provisioning/`.

## Model server (vLLM)

The `vllm` service: image `vllm/vllm-openai:v0.23.0` (the version is pinned —
the evaluation was done on it), Qwen2.5-7B-Instruct-AWQ served as
`qwen2.5-7b`, 8192-token context, 85% of GPU memory. The facade reaches it by
service name (`VLLM_BASE=http://vllm:8000/v1`), Prometheus at
`vllm:8000/metrics`.

- Weights live on the host in `~/vllm-hf-cache` (another path — `VLLM_HF_CACHE`
  in `.env`) and are not re-downloaded when the container is recreated.
- Readiness: a healthcheck on `/health` with `start_period: 300s` — the model
  loads for 2–3 minutes, until then the container is `starting`.
- Recovery: `restart: unless-stopped` — after a crash or a Docker restart vLLM
  comes back by itself.
- The facade has no `depends_on: vllm`: it starts without the model, and
  `/health` shows that generation is unavailable.

```bash
docker compose --profile gpu up -d vllm      # bring up or recreate only vLLM
docker compose logs -f vllm                  # model loading, CUDA/OOM errors
docker inspect -f '{{.State.Health.Status}}' vllm   # starting → healthy
curl http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-7b","messages":[{"role":"user","content":"ping"}]}'
```

## Question path (rag/rag.py)

1. **bge-m3** embedding of the question (text → vector for search by meaning)
   and cosine search for 12 candidates among runbook chunks
   (`corpus/runbooks/`).
2. Reranking — re-sorting the candidates with the **bge-reranker-v2-m3**
   cross-encoder: it reads the question and the chunk together and gives a
   relevance score. The embedder and reranker run on the CPU — the GPU is busy
   with the model.
3. Confidence threshold: if the best chunk scores below `RAG_MIN_SCORE=0.3`,
   the model is not called. The answer is "no confident answer in the runbooks"
   plus the three closest runbooks, and the question is flagged for the on-call
   engineer (`escalate`).
4. Context: up to two runbooks (`RAG_CTX_RUNBOOKS=2`) whose best chunk is not
   below the threshold, whole and in document order. `RAG_CTX_RUNBOOKS=0` is
   the previous mode, 8 chunks in rerank order.
5. Generation in vLLM, temperature 0.
6. The "Source" line names the runbook ranked first; it is added by code, not
   by the model.
7. Command guard: code lines that are not in the context are listed under the
   block with a ⚠️ flag. It does not catch skipped steps and warnings.

Why it is built this way — in [eval.en.md](eval.en.md). The index is cached on
disk and recomputed when runbooks change: cold indexing ~60 s, warm — instant.

```bash
pip install -r rag/requirements.txt
py rag/rag.py "Контейнер vLLM упал с OOM — как диагностировать и что сделать?"
# the assistant works in Russian: "The vLLM container crashed with OOM — how do I diagnose it and what do I do?"
```

## API facade (facade/app.py)

A thin FastAPI layer over `rag/rag.py`: it does not duplicate the retrieval
logic, it imports it.

- `POST /ask` `{"question": "..."}` — the answer and service fields:

  | Field | Contents |
  |---|---|
  | `answer` | answer text with the "Source" line and ⚠️ flags |
  | `sources` | runbooks that went into the context |
  | `cited` | the runbook named as the source |
  | `unverified` | number of command lines flagged by the guard |
  | `escalate` | `true` if the model was not called and the question went to the on-call engineer |
  | `retrieval_s`, `latency_s` | CPU retrieval time and vLLM generation time; the full time is their sum (retrieval ~16 s) |
  | `completion_tokens`, `ask_id` | answer tokens and the row number in the log |

  If vLLM is unavailable — `502 {"detail": "Upstream vLLM unreachable: ..."}`.
- `POST /feedback` `{"ask_id": 42, "rating": 1 | -1, "comment": "..."}` — the
  👍/👎 rating goes into the same log row. `404` — no such successful answer,
  `503` — the log is unavailable.
- `GET /health` — `200` if the index is loaded and vLLM responds; otherwise
  `503`.
- `GET /` — demo chat (`facade/demo.html`, a single HTML file with no external
  dependencies): question, answer with the source, a yellow banner on
  escalation, 👍/👎. The page has no logic of its own — it calls the same
  `/ask`, `/feedback` and `/health`.

Local run on the host (vLLM already listening on :8000; the address is
`VLLM_BASE`, default `http://localhost:8000/v1`):

```bash
pip install -r facade/requirements.txt
py -m uvicorn facade.app:app --port 8080       # index warm-up ~10–70 s on CPU
curl http://localhost:8080/health
curl -X POST http://localhost:8080/ask -H 'Content-Type: application/json' \
  -d '{"question":"Контейнер vLLM упал с OOM — как диагностировать?"}'
```

## Logs (Loki + Promtail)

Promtail reads the facade container's stdout via the Docker socket and ships it
to Loki with the label `job="facade"`. The whole stream is JSON (`ts`, `level`,
`logger`, `msg` and request context): uvicorn access logs are off, the
application itself writes a line per request, and model-loading progress bars
are suppressed. One log line is one valid JSON object, so filtering by fields
works reliably.

```bash
docker compose up -d loki promtail        # Loki :3100, Promtail pulls the facade logs
```

Grafana has a **Facade Logs (Loki)** dashboard: error rate and a live log
stream. Facade errors are filtered by field, not by the substring
`|= "error"`:

```logql
{job="facade"} | json | level="ERROR" | line_format "{{.ts}} {{.level}} {{.msg}}"
```

Loki rather than ELK: on a single node Loki is lighter — it indexes only labels
(`job`, `level`), not the full text, and shows up in the same Grafana as the
metrics. Elasticsearch full-text search pays off at large volumes; here it is
overkill.

## Request log (PostgreSQL)

The facade writes every processed `/ask` as a row in the `ask_log` table: time,
question, status, response time, tokens, sources, escalation, rating. The log
is auxiliary: if PostgreSQL is down, `/ask` keeps answering. On-call queries,
migrations and the resilience check — [sql.en.md](sql.en.md).
