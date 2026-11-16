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

## Watch the agent work, live

The page at http://localhost:8090 has an **Agent activity** timeline. Every step the agent takes appears as it happens, like watching an engineer work: the alert it noticed, each check it ran and what it found, its reasoning, the diagnosis with evidence, the policy check, the human decision, the action, and whether the alerts cleared. Approval can be given with **Approve / Deny buttons on the page**.

```bash
python agent/agent.py --watch --browser-approval --backend ollama
```

Then click a failure button on the page. The agent notices the alert on its own and the timeline fills in. Events are posted to the demo app by `agent/reporter.py` (best effort: if the page is down the agent carries on) and the audit log stays the source of truth. All timeline text comes from the model, so the page escapes it before display.

## Phase 4: eval

`eval/run_eval.py` injects each failure several times, runs the agent in read-only mode, scores the diagnosis against ground truth and writes `eval/RESULTS.md`:

```bash
python eval/run_eval.py --runs 3 --backend ollama     # all 5 scenarios x 3 runs; resumable with --resume
cd eval && python -m unittest                         # scoring tests
```

Scoring is a keyword match on the root cause plus an allowed-action check (see `SCENARIOS`). Two things keep the eval honest: reset clears logs and deploy history so one incident's errors cannot leak into the next, and the config-change log line does not name the fault.

## Results (qwen2.5:7b, CPU-only laptop, 15 runs)

| Scenario | Reached diagnosis | Root cause right | Action OK | Fully correct | Avg seconds |
|---|---|---|---|---|---|
| Bad release (v1.1.0) | 3/3 | 3/3 | 3/3 | 3/3 | 162 |
| Memory leak | 3/3 | 3/3 | 0/3 | 0/3 | 161 |
| Database errors | 3/3 | 3/3 | 0/3 | 0/3 | 121 |
| Slow responses | 3/3 | 0/3 | 1/3 | 0/3 | 159 |
| Dependency down | 3/3 | 3/3 | 0/3 | 0/3 | 170 |
| **Overall** | **15/15** | **12/15 (80%)** | **4/15 (27%)** | **3/15 (20%)** | **155** |

What this shows:
- The small local model usually finds the cause (80%) and always reaches a diagnosis, but it picks the wrong fix. It proposed a rollback when no bad release had shipped (leak, dependency down, slow) and `restart` for DB errors.
- It missed the slow-response incident entirely, blaming a vague "configuration change" log line.
- Guardrails on the 11 wrong-action proposals: 5 were blocked by policy (`restart` is not allowlisted; one rollback targeted an unknown version). The other 6 were rollbacks to the already-running v1.0.0, which policy allows, so they would have reached the human approval prompt. Human approval is the last line of defence, which is why it is mandatory.
- Limits: one model, 3 runs per scenario, keyword scoring, a simulated service. Not a general benchmark. Scoring counts `restart` for DB errors as wrong because it is not allowlisted; a stricter or looser rubric changes the action numbers.

Ideas to improve the numbers: a larger model or the Claude API, a prompt that says rollback only applies when a deploy preceded the incident, and tool outputs that include current vs. previous version.

## Next steps
Demo recording; compare with a stronger model.
