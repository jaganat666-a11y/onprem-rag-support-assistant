# Answer quality evaluation

[Русский](eval.md) · English

How we check that the assistant answers correctly: a fixed question set,
retrieval metrics separate from answer metrics, two blind LLM judges and a
script that checks commands. Code — [eval/](../eval/); raw answers and reports
(in Russian) — [eval/runs/](../eval/runs/), with an index of runs there.

## Results as of 2026-10-01

Setup: Qwen2.5-7B-Instruct-AWQ on an 8 GB GPU, retrieval over 12 candidates
with reranking (a second model re-sorts the retrieved chunks by meaning),
confidence threshold 0.3, up to two whole runbooks in the context,
temperature 0 (no random variation in answers), command guard.

| Metric | Result |
|---|---|
| Correct runbook ranked first | 40 of 40 |
| Correct answers, judge Sonnet 5.5 / Opus 5.5 | 32 / 26 of 40; 1 incorrect for both (q06) |
| Code lines not from the runbooks (script) | 5 in 5 answers, all flagged ⚠️ |
| Out-of-corpus questions | honest refusal 10 of 10, also 10 of 10 on the held-out set: the model is not called |
| Short chat-style questions, 10 in the corpus | the right runbook first in 9, but the threshold cuts off 8: instead of an answer, the three closest runbooks, the right one among them in 8 of 8 |
| Response time, median / 95th percentile | 25 / 45 s over 40 answers, of which CPU retrieval takes 16 s |

Same retrieval, context and prompt — a different model writes the answer:

| Who writes the answer | Where it runs | Correct, Sonnet / Opus | Code lines not from the runbooks |
|---|---|---|---|
| Qwen2.5-7B-Instruct-AWQ | own 8 GB GPU | 32 / 26 | 5 in 5 answers |
| GigaChat-2-Max | Sber cloud API | 36 / 34 | 10 in 7 answers |
| Claude Sonnet 5.5 | Anthropic cloud API | 40 / 39 | 0 |

Conclusions:

1. **Retrieval works; generation is the bottleneck.** The corpus is fictional,
   so no model knows the answers in advance. Since Sonnet with the same context
   is correct on 39–40 of 40 questions, the needed facts do reach the context.
   Qwen2.5-7B is what fits on vLLM in 8 GB together with the KV cache — the
   memory for the requests' context (9B models did not fit); only a model of a
   different class will raise the score.
2. **An invented command is visible; an omission is not.** The guard flags
   every code line that is not in the runbooks. A skipped step or warning ("L2
   only", "first check the status with the bank") is caught only by the judges.
   The only incorrect answer (q06) is exactly that: advice to refund money,
   which RB-02 forbids.
3. **Short questions hit the threshold.** Retrieval finds the right document,
   but a short question gets a low confidence score, and 8 of 10 go to the
   on-call engineer with a hint instead of an answer. An absolute threshold
   does not work on such questions.

Limitations:

- The questions were written by the corpus author, and their wording is close
  to the runbook text. The held-out set of short questions showed that
  retrieval is weaker on them.
- The corpus is small: 16 runbooks, 96 chunks. Eight random chunks would hit
  the right runbook in 42% of questions, the first random one in 6%. On
  hundreds of documents retrieval has to be measured again.
- On 40 questions, a share of correct answers around 80% is known to about
  ±12 pp (95% confidence interval), a share around 65% to ±15 pp. A difference
  under ~10 pp is not a finding.
- Both judges are Claude, and Sonnet also graded its own answers. The judges
  saw the ⚠️ flags: without them the 7B gets 30 / 25 correct. The manual check
  of the judge covered 10 answers, and the marks were made with its verdict
  visible.
- The 7B runs at temperature 0, as in the facade; GigaChat and Sonnet at their
  API defaults. Each row is a single run; variation is ±1–2 answers.
- Failures were analyzed on the same set they were measured on. The fixes are
  general, not tailored to specific questions, but there is no separate judged
  set for them.
- Local models newer than the 7B were not tested: Qwen3.5-9B and GigaChat 3.1
  Lightning in vLLM builds do not fit in 8 GB and need a different inference
  server (llama.cpp and GGUF).
- Timing depends on hardware: retrieval runs on the laptop CPU (Ryzen 7 6800H,
  8 cores), generation on an RTX 3070 Ti Laptop 8 GB.

## Methodology

### Question set

`eval/questions.jsonl` — 50 questions phrased the way an on-call engineer asks
them ("a customer writes…", "in the logs I see…"):

- 40 in the corpus. Each has an expected runbook, a reference answer and 1–3
  key facts (table name, command, metric) that must appear. All 16 runbooks are
  covered, 1–4 questions each; 2 questions need two runbooks. 8 questions are
  cross-lingual: 6 Russian questions to English runbooks (RB-06, RB-13, RB-16)
  and 2 English questions to Russian runbooks.
- 10 outside the corpus — neighboring topics where a model easily invents a
  plausible procedure: PostgreSQL point-in-time recovery, consumer lag in
  Kafka, TLS certificate renewal, Kubernetes, on-call terms. The correct answer
  is to say that the runbooks don't cover it.

`eval/questions_holdout.jsonl` — a held-out set that nothing was tuned on. 10
in-corpus questions are short and vague, like in a work chat ("paid by QR, the
operation has been processing for half an hour", "the customer sees zero in the
mini app, but the money shows in MAX"), without words from runbook titles;
three of them fit two runbooks. 10 questions are neighboring topics outside the
corpus (a top-up limit, reissuing a plastic card, a Grafana alert, KV cache for
a 32k-token context, chargeback, 3-D Secure); that the corpus has no answer was
verified by text search.

### Metrics

| Metric | What it shows | Computed by |
|---|---|---|
| Correct runbook at rank 1 (hit@1) | quality of retrieval and reranking | script |
| Correct runbook among the top 8 chunks (hit@8) | whether retrieval found the document at all; only runbooks above the threshold go into the context | script |
| Citation of the correct runbook | the on-call engineer can check the source | script |
| Key fact coverage | share of the set's facts found in the answer | script |
| Correct / partial / incorrect | verdict on substance, checked against the full corpus text | judge |
| Honest refusal outside the corpus | said "not in the runbooks" and did not invent a procedure | judge |
| Answers with fabrication | commands, numbers, table names that are not in the corpus | judge |
| False refusals | "no information" although the corpus has it | judge |
| Citation of an unrelated runbook | names a document nothing was taken from | script and judge |
| Answers with Chinese characters | the model slipped into Chinese | script |
| Code lines not from the runbooks | a code-block line that is not in the corpus | script |
| Time, median and 95th percentile | full `/ask` time, retrieval and generation separately | script |

Two rubric rules lower the verdict regardless of substance: an answer that is
not in Russian, or that cites a runbook from which no step was taken, is at
most "partial". The on-call engineer reads Russian and follows the citation to
check the source; a wrong link costs time in the middle of an incident.

Retrieval metrics are kept separate from answer metrics: on a failure it is
immediately clear where to fix — the document was not found (retrieval) or was
found but the model answered badly (generation).

### Judges

The judge gets the full text of all 16 runbooks, the reference and the
assistant's answer, and checks every command and number against the source.
All 50 answers go in one call (about 60k tokens in and 7k out) via the Claude
Code CLI (`claude -p`, without tools or MCP). The main judge is Claude Sonnet
5.5; since Oct 1 every run is also graded blind by a second judge, Claude Opus
5.5, which does not see the first one's verdicts. An external judge is
acceptable only because the corpus and questions are synthetic; for real data
the judge must be local, like the assistant itself.

Code lines are checked by a script without a model, `eval/check_commands.py`:
every line from the answer's code blocks is looked up in the runbook text.
Whitespace is collapsed, comments are skipped, and a placeholder like
`<user_id>` is accepted only in place of a concrete value.

Two traps found along the way:

- The judge model is set by an exact id, and the report records the id the CLI
  actually called. In the CLI installed at the time, the alias `--model sonnet`
  meant `claude-sonnet-4-6`, not the latest model: an alias moves over time,
  and numbers from different runs stop being comparable.
- The judge is called with `--safe-mode`. Without it, `claude -p` adds the CLI
  user's personal settings file (`~/.claude/CLAUDE.md`) to the context even
  when a system prompt is set. The judges of runs before Oct 1 12:06 ran with
  this file, including the path 23 → 26 → 33.

The refusal heuristic in the script is crude (it looks for phrases like "not in
the runbooks"), so the final refusal count is set by the judge. `judge.py` also
builds a manual check sheet, `manual_check.html`, with 10 answers of all
verdict types.

### How to run

```bash
# facade :8080 and vLLM :8000 are up
py eval/run_eval.py --tag qwen2.5-7b        # ~20 min for 50 questions, metrics without a judge
py eval/run_eval.py --questions eval/questions_holdout.jsonl --tag holdout   # held-out set
py eval/judge.py eval/runs/<run folder>     # verdicts, report.md, manual check sheet
py eval/judge.py <folder> --model claude-opus-5-5 --second  # second judge, the main report is untouched
py eval/judge.py <folder> --report-only      # rebuild the report without calling the judge
py eval/check_commands.py <folder>           # code lines from answers against the runbook text
py eval/regen.py <folder> --gen claude-sonnet-5-5   # same context, another model writes the answer
py eval/regen.py <folder> --gen GigaChat-2-Max      # same via the GigaChat API (key in .env)
```

The GigaChat key is `GIGACHAT_AUTH_KEY` in `.env` (not committed). The root
certificate of the Russian Ministry of Digital Development is used only for
connections to GigaChat (`eval/certs/`).

## Measurement history

Each change is a separate run of the same set; raw answers and reports are in
the [run index](../eval/runs/README.en.md). Correct answers are out of 40
in-corpus questions, judge Sonnet 5.5 / Opus 5.5.

| Date | What changed | Result | Takeaway |
|---|---|---|---|
| Sep 30 | first measurement: 24 candidates, no threshold, temperature 0.2 | 20 correct; out-of-corpus refusal 4 of 10; answer 41.6 s | the right runbook first 40 of 40 — generation is what fails |
| Sep 30 | 12 candidates instead of 24 | 23 correct; answer 24.2 s | quality within noise, 42% faster — became the default |
| Sep 30 | no reranking | runbook first 39 of 40; answer 8.1 s | faster, but the confidence signal is lost (below) |
| Sep 30 | confidence threshold 0.3: below it the model is not called | out-of-corpus refusal 10 of 10, fabrications and Chinese characters 0 | fabrication outside the base is cut off by code, not by the prompt |
| Sep 30 | held-out set of short questions | the threshold cuts off 8 of 10 in-corpus; the right runbook among the three suggested — 8 of 8 | below the threshold — a hint instead of a flat refusal |
| Oct 1 | prompt and temperature 0 | 26 correct | +3 is noise; temperature 0 kept for repeatability |
| Oct 1 | code, not the model, adds the source citation | 33 correct | extraneous citations 12 → 0, which by the rubric is +7 correct; don't ask the model for what code does exactly |
| Oct 1 | second blind judge and a script check of commands | 33 / 26 | agreement on 43 of 50; in all 7 disagreements Opus is stricter |
| Oct 1 | context — up to two whole runbooks | 30 / 25 | no gain for the 7B: the errors reshuffled |
| Oct 1 | command guard | 32 / 26 | all 5 invented lines flagged ⚠️ |
| Oct 1 | same context, GigaChat-2-Max and Sonnet answer | 36 / 34 and 40 / 39 | retrieval works, the model is the bottleneck |
| Oct 1 | stricter guard: whole lines, placeholders, inline commands | the same lines flagged in 150 answers | the gaps were formal, no false flags |
| Oct 1 | held-out set on the current configuration | the same 8 of 10 cut off | the limit is the threshold, not the context |

Three findings that shaped the configuration:

- **The first judge score was inflated.** Sonnet 4.6 with a rubric lacking the
  language and citation rules gave 31 correct instead of 20–23. q35 is telling:
  the SQL is correct but the labels are swapped, and the on-call engineer would
  have killed the wrong process — 4.6 and the human accepted it, 5.5 did not.
  The human saw the judge's verdict during the check and agreed with it, hence
  the second blind judge and the script.
- **The reranker is needed for confidence, not accuracy.** Without it the right
  document is still found, but the cosine scores in the corpus (from 0.55) and
  outside it (up to 0.63) overlap. The reranker score separates them cleanly:
  in the corpus no lower than 0.455, outside it no higher than 0.112 — the
  threshold rests on this.
- **An absolute threshold does not carry over to short questions.** For "the
  customer sees zero in the mini app" the right runbook is first, but the score
  is low: in the corpus from 0.021, outside it up to 0.133 — no threshold can
  separate them. Tuning the threshold on the held-out set would spoil the set,
  so below the threshold the assistant gives a hint, not a refusal.
