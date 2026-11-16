"""
Eval harness: how often does the agent diagnose injected incidents correctly?

For each scenario and run it: resets the demo app, injects one failure, waits for an alert,
runs the (read-only) agent, scores the diagnosis against ground truth, and resets again.
Results are saved after every run, so a long eval can be stopped and resumed.

    python eval/run_eval.py --runs 3                       # all scenarios, 3 runs each
    python eval/run_eval.py --runs 1 --scenarios leak slow
    python eval/run_eval.py --runs 3 --resume              # skip runs already in results.json

Scoring is deliberately simple and transparent (keyword match on the root cause plus an
allowed-action check); see SCENARIOS. It measures whether the diagnosis names the right
cause and proposes a sensible action, not the quality of the prose.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "agent"))
import agent  # noqa: E402
import guardrails  # noqa: E402

RESULTS_JSON = os.path.join(HERE, "results.json")
RESULTS_MD = os.path.join(HERE, "RESULTS.md")

# inject: (path, body) sent to the demo app. cause_keywords: any match in root_cause counts.
SCENARIOS = {
    "bad_deploy": {
        "label": "Bad release (v1.1.0)",
        "inject": ("/admin/deploy", {"version": "v1.1.0", "note": "release with checkout changes"}),
        "cause_keywords": ["v1.1.0", "deploy", "release", "keyerror", "discount_code", "checkout"],
        "actions": {"rollback"}, "rollback_target": "v1.0.0",
    },
    "leak": {
        "label": "Memory leak",
        "inject": ("/admin/fault", {"mode": "leak"}),
        "cause_keywords": ["memory", "leak"],
        "actions": {"clear_fault", "restart", "restart_service", "escalate_to_human"},
    },
    "rogue_process": {
        "label": "Runaway process",
        "inject": ("/admin/fault", {"mode": "rogue_process"}),
        "cause_keywords": ["report-export", "runaway", "rogue", "process"],
        "actions": {"kill_process"}, "kill_target": "report-export",
    },
    "errors": {
        "label": "Database errors",
        "inject": ("/admin/fault", {"mode": "errors"}),
        "cause_keywords": ["database", "db ", "connection pool", "pool exhausted"],
        "actions": {"clear_fault", "escalate_to_human"},
    },
    "slow": {
        "label": "Slow responses",
        "inject": ("/admin/fault", {"mode": "slow"}),
        "cause_keywords": ["latency", "slow", "response time"],
        "actions": {"clear_fault", "escalate_to_human"},
    },
    "inventory_down": {
        "label": "Dependency down",
        "inject": ("/admin/fault", {"mode": "inventory_down"}),
        "cause_keywords": ["inventory", "dependency", "upstream"],
        "actions": {"clear_fault", "escalate_to_human"},
    },
}


def score(scenario, diagnosis):
    """Score one diagnosis against a scenario's ground truth."""
    spec = SCENARIOS[scenario]
    if diagnosis is None:
        return {"reached": False, "cause_ok": False, "action_ok": False, "correct": False}
    cause = diagnosis["root_cause"].lower()
    cause_ok = any(k in cause for k in spec["cause_keywords"])
    action = diagnosis["proposed_action"]
    action_ok = action["type"] in spec["actions"]
    if "rollback_target" in spec and action["type"] == "rollback":
        action_ok = action_ok and action.get("target_version") == spec["rollback_target"]
    if "kill_target" in spec and action["type"] == "kill_process":
        action_ok = action_ok and action.get("target") == spec["kill_target"]
    return {"reached": True, "cause_ok": cause_ok, "action_ok": action_ok, "correct": cause_ok and action_ok}


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "-"


def summarize(results):
    """Return rows of per-scenario stats plus an overall row."""
    rows = []
    groups = {name: [r for r in results if r["scenario"] == name] for name in SCENARIOS}
    groups = {k: v for k, v in groups.items() if v}
    for name, rs in list(groups.items()) + [("ALL", results)]:
        n = len(rs)
        timed = [r["seconds"] for r in rs if r.get("seconds")]
        rows.append({
            "scenario": SCENARIOS[name]["label"] if name in SCENARIOS else "Overall",
            "runs": n,
            "reached": pct(sum(r["score"]["reached"] for r in rs), n),
            "cause": pct(sum(r["score"]["cause_ok"] for r in rs), n),
            "action": pct(sum(r["score"]["action_ok"] for r in rs), n),
            "correct": pct(sum(r["score"]["correct"] for r in rs), n),
            "avg_s": f"{sum(timed) / len(timed):.0f}" if timed else "-",
        })
    return rows


def render_markdown(results, model, backend):
    rows = summarize(results)
    lines = [f"# Eval results: {backend} / {model}", "",
             f"{len(results)} runs. 'Correct' = right root cause AND an allowed action.", "",
             "| Scenario | Runs | Reached diagnosis | Root cause right | Action OK | Fully correct | Avg seconds |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['scenario']} | {r['runs']} | {r['reached']} | {r['cause']} | {r['action']} "
                     f"| {r['correct']} | {r['avg_s']} |")
    lines += ["", "Scoring: keyword match on `root_cause` plus an allowed-action check (see `SCENARIOS` in "
                  "`run_eval.py`). Results are for one model on one machine and are not a general benchmark."]
    return "\n".join(lines) + "\n"


def reset():
    guardrails.http("POST", "/admin/reset", {})
    time.sleep(3)


def wait_for_alert(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        alerts = agent.fetch("/api/alerts")
        if alerts:
            return [a["alert"] for a in alerts]
        time.sleep(2)
    return []


def make_client(backend):
    if backend == "anthropic":
        import anthropic
        return anthropic.Anthropic(), agent.MODEL
    import ollama_client
    return ollama_client.OllamaClient(), os.getenv("AGENT_MODEL", ollama_client.DEFAULT_MODEL)


def save(results, model, backend):
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump({"backend": backend, "model": model, "results": results}, f, indent=2)
    with open(RESULTS_MD, "w", encoding="utf-8") as f:
        f.write(render_markdown(results, model, backend))


def run_one(client, model, name, run_no):
    reset()
    path, body = SCENARIOS[name]["inject"]
    guardrails.http("POST", path, body)
    alerts = wait_for_alert()
    record = {"scenario": name, "run": run_no, "alerts": alerts}
    if not alerts:
        record.update(error="no alert fired", diagnosis=None, seconds=None, score=score(name, None))
        return record
    start = time.time()
    try:
        diagnosis = agent.run_triage(client, verbose=False, model=model)
        record["error"] = None
    except Exception as e:  # a crashed run is a failed run, not a crashed eval
        diagnosis, record["error"] = None, f"{type(e).__name__}: {e}"
    record.update(diagnosis=diagnosis, seconds=round(time.time() - start), score=score(name, diagnosis))
    return record


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--scenarios", nargs="+", choices=list(SCENARIOS), default=list(SCENARIOS))
    p.add_argument("--backend", choices=["anthropic", "ollama"],
                   default="anthropic" if os.getenv("ANTHROPIC_API_KEY") else "ollama")
    p.add_argument("--resume", action="store_true", help="skip runs already in results.json")
    args = p.parse_args()

    client, model = make_client(args.backend)
    results = []
    if args.resume and os.path.exists(RESULTS_JSON):
        with open(RESULTS_JSON, encoding="utf-8") as f:
            results = json.load(f)["results"]
    done = {(r["scenario"], r["run"]) for r in results}
    print(f"Eval: {args.backend} / {model}, scenarios={args.scenarios}, runs={args.runs}")

    for run_no in range(1, args.runs + 1):
        for name in args.scenarios:
            if (name, run_no) in done:
                continue
            print(f"[run {run_no}] {name} ...", flush=True)
            rec = run_one(client, model, name, run_no)
            results.append(rec)
            s = rec["score"]
            print(f"    alerts={rec['alerts']} reached={s['reached']} cause_ok={s['cause_ok']} "
                  f"action_ok={s['action_ok']} {rec['seconds']}s {rec['error'] or ''}", flush=True)
            save(results, model, args.backend)
    reset()
    print("\n" + render_markdown(results, model, args.backend))
    print(f"Saved {RESULTS_JSON} and {RESULTS_MD}")


if __name__ == "__main__":
    main()
