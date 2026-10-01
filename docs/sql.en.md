# SQL diagnostics: the `ask_log` request log

[Русский](sql.md) · English

The facade writes every processed `/ask` as a row in PostgreSQL (the `postgres`
service, schema — [db/init.sql](../db/init.sql)). When someone complains that
"the assistant is slow" or "returns errors", the answer comes from plain SQL —
below is a working set of queries.

Connecting to the database (from inside the container, the `psql` client is
already there):

```bash
docker compose exec postgres psql -U facade -d asklog
```

Useful in `psql`: `\dt` — list tables, `\d ask_log` — table schema, `\x` —
vertical output for wide rows, `\q` — quit.

## 1. What is happening right now: the last 20 requests

```sql
SELECT ts, status, latency_ms, left(question, 60) AS question, error
FROM ask_log
ORDER BY ts DESC
LIMIT 20;
```

## 2. How many errors in the last hour (the first question on an alert)

```sql
SELECT count(*) FILTER (WHERE status >= 500) AS errors,
       count(*)                              AS total
FROM ask_log
WHERE ts > now() - interval '1 hour';
```

## 3. Hourly trend over a day: when did it start?

```sql
SELECT date_trunc('hour', ts)                    AS hour,
       count(*)                                  AS total,
       count(*) FILTER (WHERE status >= 500)     AS errors,
       round(avg(latency_ms)::numeric, 0)        AS avg_ms
FROM ask_log
WHERE ts > now() - interval '24 hours'
GROUP BY 1
ORDER BY 1;
```

## 4. Latency tail: p95 and the slowest requests

The average hides problems — look at the 95th percentile (the slowest 5% of
requests take longer than it) and at the slow requests themselves.
`latency_ms` is the full response time at the facade:

```sql
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM ask_log
WHERE status = 200 AND ts > now() - interval '24 hours';

SELECT ts, latency_ms, completion_tokens, left(question, 60) AS question
FROM ask_log
WHERE status = 200
ORDER BY latency_ms DESC
LIMIT 10;
```

## 5. Which runbooks actually work (how often they get into the context)

`sources` is an array; `unnest` expands it into rows, then a regular GROUP BY:

```sql
SELECT unnest(sources) AS runbook, count(*) AS hits
FROM ask_log
WHERE status = 200
GROUP BY 1
ORDER BY hits DESC
LIMIT 10;
```

## 6. Output tokens per day

```sql
SELECT count(*)                    AS answers,
       sum(completion_tokens)      AS tokens,
       round(avg(completion_tokens)::numeric, 0) AS avg_tokens
FROM ask_log
WHERE status = 200 AND ts > now() - interval '24 hours';
```

The log stores only output tokens — 0.27k per answer on average. It does not
record input tokens (question and runbooks, ~1.8k), so the full usage and the
cost of an answer cannot be computed from it; the cost estimate is in
[pilot.en.md](pilot.en.md).

## 7. User ratings: share of 👎 by day

`/ask` returns `ask_id`, and the user rates the answer via
`POST /feedback {"ask_id": 42, "rating": -1, "comment": "..."}` — the rating
goes into the same log row (columns `rating`, `feedback_comment`,
`feedback_ts`).

```sql
SELECT date_trunc('day', ts)                AS day,
       count(*)                             AS answers,
       count(*) FILTER (WHERE rating = 1)   AS up,
       count(*) FILTER (WHERE rating = -1)  AS down
FROM ask_log
WHERE status = 200 AND ts > now() - interval '7 days'
GROUP BY 1
ORDER BY 1;
```

## 8. Bad answers: what to review

```sql
SELECT id, feedback_ts, left(question, 70) AS question, sources, feedback_comment
FROM ask_log
WHERE rating = -1
ORDER BY feedback_ts DESC
LIMIT 20;
```

`sources` shows where to look: the right runbook is not in the list — a
retrieval miss; it is there but the answer is wrong — a generation miss; the
base has no such topic at all — the model should have refused.

## 9. What went to the on-call engineer: questions without a confident answer

If retrieval found no chunk scoring above the `RAG_MIN_SCORE` threshold, the
model is not called and the row is flagged `escalated = true`. Two kinds of
questions end up here: gaps in the base (candidates for new runbooks) and
in-scope questions that are too short — on the held-out evaluation set 8 of 10
such questions ended up here ([eval.en.md](eval.en.md)).

```sql
SELECT ts, left(question, 80) AS question
FROM ask_log
WHERE escalated AND ts > now() - interval '7 days'
ORDER BY ts DESC;
```

## Bad answer → candidate for the test set

The quality evaluation ([eval.en.md](eval.en.md)) runs on a fixed set,
`eval/questions.jsonl`. Questions rated 👎 are exported as drafts in the same
format:

```bash
docker compose exec -T postgres psql -U facade -d asklog -At -c "
  SELECT json_build_object('id', 'fb' || id, 'kind', NULL, 'runbooks', '[]'::json,
                           'question', question, 'reference', '',
                           'facts', '[]'::json, 'feedback_comment', feedback_comment)
  FROM ask_log WHERE rating = -1 AND feedback_ts > now() - interval '7 days'
  ORDER BY id" > eval/candidates.jsonl
```

Then by hand: `kind` (`in` — the corpus has the answer, `out` — it doesn't, a
refusal is expected), `runbooks`, the `reference` answer and the key `facts`;
finished lines move to `eval/questions.jsonl`. This way every caught failure
becomes a regression test: the next run shows whether the fix worked.

## Migrations

`db/init.sql` runs only on the first start with an empty volume. New columns on
an already running setup are added by migrations (safe to re-run):

```bash
docker compose exec -T postgres psql -U facade -d asklog < db/migrations/002_feedback.sql   # 👍/👎 ratings
docker compose exec -T postgres psql -U facade -d asklog < db/migrations/003_escalated.sql  # escalation flag
```

## Resilience to a database failure

The log is an auxiliary layer: if PostgreSQL is down, `/ask` keeps answering,
and the facade writes `WARNING ask_log insert failed: ...` to its log (visible
in Loki: `{job="facade"} | json | level="WARNING"`). Checked with one command:
`docker compose stop postgres` → `/ask` still returns 200 → `start` — records
resume.
