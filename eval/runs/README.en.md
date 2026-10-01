# Evaluation runs

[Русский](README.md) · English

Raw answers and verdicts for all measurements in
[docs/eval.en.md](../../docs/eval.en.md). A folder name is the run's date and
time plus a change tag. Correct answers are out of 40 in-corpus questions; the
Sonnet 5.5 judge comes first, Opus 5.5 second. Reports inside the folders are in
Russian.

| Folder | What changed | Result | Topic in eval.en.md |
|---|---|---|---|
| `2026-09-30_1532_qwen2.5-7b` | first measurement: 24 candidates, no threshold, temperature 0.2 | 20 correct (31 from Sonnet 4.6 with rubric v1) | first measurement; judge score |
| `2026-09-30_1620_qwen2.5-7b_cand12` | 12 candidates | 23 correct, answer 24.2 s | first measurement |
| `2026-09-30_1644_qwen2.5-7b_norerank` | no reranking, no judge | runbook first 39 of 40, answer 8.1 s | first measurement |
| `2026-09-30_1654_qwen2.5-7b_gate_out` | threshold 0.3, only the 10 out-of-corpus questions | refusal 10 of 10 | confidence threshold |
| `2026-09-30_2342_holdout` | held-out set, refusal below the threshold | 8 of 10 in-corpus cut off | held-out set |
| `2026-09-30_2357_holdout_suggest` | below the threshold — three runbooks suggested | the right one among the suggested 8 of 8 | held-out set |
| `2026-10-01_0931_prompt_t0` | prompt and temperature 0 | 26 correct | prompt, temperature 0 |
| `2026-10-01_1006_cite_code` | code adds the source citation; second judge | 33 / 26 | prompt; second judge |
| `2026-10-01_1153_whole_rb` | context — up to two whole runbooks | 30 / 25 | context and guard |
| `2026-10-01_1216_regen_claude-sonnet-5-5` | same context, Claude Sonnet 5.5 answers | 40 / 39 | model comparison |
| `2026-10-01_1243_regen_GigaChat-2-Max` | same context, GigaChat-2-Max answers | 36 / 34 | model comparison |
| `2026-10-01_1249_regen_guard` | answers of `1153_whole_rb` with guard flags — **current configuration** | 32 / 26 | context and guard; results |
| `2026-10-01_1542_holdout_whole_rb` | held-out set on the current configuration | the same 8 of 10 cut off | held-out set on the current configuration |

Files in a folder:

- `answers.jsonl` — question, answer, retrieved chunks with scores, retrieval
  and generation time;
- `summary.json` — script metrics (retrieval, facts, time) and the judge's
  result;
- `report.md`, `grades.jsonl` — report and verdicts of the Sonnet 5.5 judge;
  `report_<model>.md`, `grades_<model>.jsonl` — the second judge;
- `commands.md`, `commands.jsonl` — code lines not from the runbooks
  (`check_commands.py`);
- `manual_check.html` — the judge's manual check sheet for 10 answers.

Run logs (`*.log`) are not committed.
