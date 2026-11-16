# Eval results: ollama / qwen2.5:7b

15 runs. 'Correct' = right root cause AND an allowed action.

| Scenario | Runs | Reached diagnosis | Root cause right | Action OK | Fully correct | Avg seconds |
|---|---|---|---|---|---|---|
| Bad release (v1.1.0) | 3 | 100% | 100% | 100% | 100% | 162 |
| Memory leak | 3 | 100% | 100% | 0% | 0% | 161 |
| Database errors | 3 | 100% | 100% | 0% | 0% | 121 |
| Slow responses | 3 | 100% | 0% | 33% | 0% | 159 |
| Dependency down | 3 | 100% | 100% | 0% | 0% | 170 |
| Overall | 15 | 100% | 80% | 27% | 20% | 155 |

Scoring: keyword match on `root_cause` plus an allowed-action check (see `SCENARIOS` in `run_eval.py`). Results are for one model on one machine and are not a general benchmark.
