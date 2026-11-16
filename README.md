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

## Next phases
2. Read-only agent: Claude API tool use over alerts, metrics, logs, deploys; outputs root cause, evidence, proposed action, confidence.
3. Guarded actions with human approval and an audit log.
4. Eval over injected incidents, README diagram, demo GIF.
