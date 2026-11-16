# Triage Agent: demo app (Phase 1)

A small simulated "Shop" service you can break on purpose, so a monitoring agent has real incidents to investigate. Python standard library only, no installs.

## Run it

```bash
cd demo_app
PORT=8090 python app.py        # PowerShell: $env:PORT=8090; python app.py
```

Open http://localhost:8090 for the live control panel.

## Failure scenarios

| Button / API call | What happens | Alert that fires |
|---|---|---|
| Memory leak (`fault=leak`) | Each request retains 1 MB | HighMemory |
| DB errors (`fault=errors`) | ~50% of requests return 500, "connection pool exhausted" | HighErrorRate |
| Slow responses (`fault=slow`) | 1.2-2.5 s per request | HighLatencyP95 |
| Dependency down (`fault=inventory_down`) | Every order returns 502, "inventory unreachable" | HighErrorRate |
| Deploy bad release (`v1.1.0`) | ~40% 500s with a `KeyError` in checkout; shows in deploy history | HighErrorRate |

Fix paths for the agent to learn: rollback to `v1.0.0`, clear the fault, or reset.

## API the agent will use

| Endpoint | Purpose |
|---|---|
| `GET /api/alerts` | Currently firing alerts |
| `GET /api/metrics` | req/s, error rate, p95 latency, memory |
| `GET /api/logs?n=50` | Recent structured logs |
| `GET /api/deploys` | Deploy history (the "what changed?" clue) |
| `POST /admin/deploy` `{"version":"v1.0.0"}` | Rollback action |
| `POST /admin/fault` `{"mode":"none"}` | Clear-fault action |

Logs and deploy history are also written to `data/app.log` and `data/deploys.jsonl`.

## Phase 2: read-only triage agent

`agent/agent.py` investigates firing alerts with Claude tool use. It has four read-only tools (alerts, metrics, logs, deploys) and one `submit_diagnosis` tool that returns the root cause, evidence, a proposed action and a confidence score. It cannot change anything. Diagnoses are appended to `data/diagnoses.jsonl`.

```bash
pip install anthropic
export ANTHROPIC_API_KEY=...     # PowerShell: $env:ANTHROPIC_API_KEY="..."
python agent/agent.py            # triage once (break something in the UI first)
python agent/agent.py --watch    # poll every 10 s
```

Tests use a scripted fake model, so they need no API key or running service:

```bash
cd agent && python -m unittest -v
```

## No API key? Run on a free local model

The agent can use Ollama instead of the Claude API. Install Ollama from https://ollama.com, then:

```bash
ollama pull qwen2.5:7b
python agent/agent.py --backend ollama      # also the default when ANTHROPIC_API_KEY is not set
```

Set `AGENT_MODEL` to try another tool-capable model. `agent/ollama_client.py` translates between the agent's tool-use format and Ollama's chat API, so the loop and guardrails are identical on both backends. Measured on a CPU-only laptop (WSL, 12 threads), `qwen2.5:7b` diagnosed the bad-deploy incident correctly in about 4 minutes; a GPU or a hosted model is much faster. If a local model writes its answer as JSON text instead of calling the tool, the agent accepts it only if it passes the same validation. Local 7B models are less reliable than Claude; the guardrails and the confidence floor exist for that reason.

## Phase 3: guarded actions

The model only proposes; `agent/guardrails.py` decides whether anything runs.

| Guardrail | Rule |
|---|---|
| Allowlist | Only `rollback` and `clear_fault`. Anything else (for example `restart`) escalates to a human |
| Known-good target | Rollback only to versions in `KNOWN_GOOD_VERSIONS` |
| Confidence floor | Below 70% the agent escalates and does not even ask for approval |
| Rate limit | At most 3 executed actions per hour |
| Human approval | Explicit `y` required, default is No |
| Verification | After acting, alerts are re-checked for up to 45 s; if they persist, it escalates |
| Audit log | Every step (diagnosis, approved/denied, executed, verified) goes to `data/audit.jsonl` |

```bash
python agent/agent.py                  # diagnose, ask approval, act, verify
python agent/agent.py --dry-run        # approval flow, but never executes
python agent/agent.py --diagnose-only  # Phase 2 behaviour
```

## Phase 4: eval

`eval/run_eval.py` injects each failure several times, runs the agent in read-only mode, scores the diagnosis against ground truth and writes `eval/RESULTS.md`:

```bash
python eval/run_eval.py --runs 3 --backend ollama     # all 5 scenarios x 3 runs; resumable with --resume
cd eval && python -m unittest                         # scoring tests
```

Scoring is a keyword match on the root cause plus an allowed-action check (see `SCENARIOS`). Two things keep the eval honest: reset clears logs and deploy history so one incident's errors cannot leak into the next, and the config-change log line does not name the fault.

## Next steps
Demo recording and results table.
