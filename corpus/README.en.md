# Runbook corpus (RAG source)

[Русский](README.md) · English

Operational runbooks — step-by-step instructions for typical incidents that the
assistant answers from. This is the service's knowledge base, not its code.

## Important: the data is synthetic

All runbooks and any identifiers in them (user_id, txn_id, amounts, IP, e-mail,
phone numbers, card numbers) are **fictional**. There is no real personal or
payment data here, and there must not be any — the corpus was written
specifically for public release. Card numbers are not given even masked, only
the last four digits in the `****1111` format.

## Structure

- `runbooks/` — atomic runbooks, one incident per file (`rb-NN-slug.md`). Each
  follows a single template: Symptom → Scope and impact → Diagnostics →
  Resolution → Escalation (L1→L2→L3) → Related.
- Languages: 13 runbooks are in Russian, 3 (RB-06, RB-13, RB-16) in English. The
  mix is intentional: the bge-m3 embedder is multilingual, and RAG retrieval is
  tested in both languages — a Russian question must find an English runbook.

Topics:

- **Payment scenarios** (webhooks, idempotency, top-ups via SBP — Russia's
  Faster Payments System, auto-conversion, double charge, card issuance,
  statement reconciliation).
- **Service infrastructure** (vLLM, latency/KV cache, FastAPI facade,
  Prometheus, Loki/Promtail, PostgreSQL, disk/CrashLoop, CI).

## How it is indexed

The RAG layer (bge-m3 + reranker, index on CPU) reads `runbooks/`, splits the
files into chunks and builds a vector index. The README files in this folder
are not indexed.
