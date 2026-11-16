"""
Triage Agent (Phase 2: read-only).

When the shop service has firing alerts, this agent investigates using read-only
tools (alerts, metrics, logs, deploy history) and submits a structured diagnosis:
root cause, evidence, proposed action and confidence. It never changes anything;
Phase 3 adds guarded actions behind human approval.

Usage:
    set ANTHROPIC_API_KEY=...            (PowerShell: $env:ANTHROPIC_API_KEY="...")
    python agent.py                      # triage once
    python agent.py --watch              # poll every 10 s and triage new incidents
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

import guardrails
from reporter import Reporter, no_report

SHOP_URL = os.getenv("SHOP_URL", "http://localhost:8090")
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-5-5")
MAX_TURNS = 8
OUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "diagnoses.jsonl")

SYSTEM_PROMPT = """You are an SRE triage agent for a service called "shop".
An alert is firing. Investigate with the read-only tools, then call submit_diagnosis exactly once.

Method:
1. Start with get_alerts and get_metrics to see what is wrong and how bad.
2. Read recent logs for error messages, and check get_deploys: did a deploy happen
   shortly before the problem began? Compare log version tags with the deploy history.
3. Form the most likely root cause from evidence only. Do not invent facts.

Proposed action must be one of: rollback (also set target_version to the last good
version from the deploy history), clear_fault, restart, escalate_to_human, none.
Your action is only a proposal: a policy layer and a human approve it before anything runs.
Do not write long explanations. When you have enough evidence, call submit_diagnosis
directly, with a short root_cause and 2-4 short evidence items. Use escalate_to_human when evidence is weak or conflicting.
Confidence is 0.0-1.0 and must reflect how conclusive the evidence is."""

TOOLS = [
    {"name": "get_alerts", "description": "List currently firing alerts.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_metrics",
     "description": "Current rolling metrics: requests/sec, 5xx error rate %, p95 latency ms, memory MB, running version.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "get_logs", "description": "Most recent structured log entries (newest last).",
     "input_schema": {"type": "object", "properties": {
         "n": {"type": "integer", "description": "How many entries, 1-100", "default": 30}}}},
    {"name": "get_deploys", "description": "Deploy history with timestamps and versions: what changed recently.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "submit_diagnosis",
     "description": "Submit the final diagnosis. Call exactly once when the investigation is complete.",
     "input_schema": {"type": "object", "properties": {
         "root_cause": {"type": "string"},
         "evidence": {"type": "array", "items": {"type": "string"},
                      "description": "Specific observations that support the root cause"},
         "proposed_action": {"type": "object", "properties": {
             "type": {"type": "string", "enum": ["rollback", "clear_fault", "restart", "escalate_to_human", "none"]},
             "detail": {"type": "string"},
             "target_version": {"type": "string", "description": "Required for rollback: the version to roll back to"}},
             "required": ["type", "detail"]},
         "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
      "required": ["root_cause", "evidence", "proposed_action", "confidence"]}},
]


def fetch(path):
    with urllib.request.urlopen(SHOP_URL + path, timeout=5) as r:
        return json.loads(r.read())


ACTION_TYPES = {"rollback", "clear_fault", "restart", "escalate_to_human", "none"}


def valid_diagnosis(d):
    """Check a diagnosis has the fields and value ranges the guardrails rely on."""
    try:
        a = d["proposed_action"]
        ok = (isinstance(d["root_cause"], str) and isinstance(d["evidence"], list)
              and a["type"] in ACTION_TYPES and isinstance(a.get("detail", ""), str)
              and isinstance(d["confidence"], (int, float)) and 0 <= d["confidence"] <= 1)
    except (KeyError, TypeError, AttributeError):
        return False
    if ok:
        a.setdefault("detail", "")
    return ok


def extract_diagnosis(text):
    """Fallback for models that write the diagnosis as JSON text instead of calling the tool.

    Local models sometimes answer in prose plus a ```json block. We accept that only if it
    parses and passes the same validation as a tool call; the guardrails still apply.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(obj, dict) and "root_cause" in obj and valid_diagnosis(obj):
            return obj
    return None


def summarize_logs(entries):
    """Collapse repeated log lines into {level, version, msg, count, first, last}.

    A failing service logs the same error hundreds of times; sending each one wastes
    context (and, on a small local model, minutes of CPU time) without adding evidence.
    """
    groups = {}
    for e in entries:
        key = (e["level"], e["version"], e["msg"])
        g = groups.setdefault(key, {"level": e["level"], "version": e["version"], "msg": e["msg"],
                                    "count": 0, "first": e["ts"][11:19]})
        g["count"] += 1
        g["last"] = e["ts"][11:19]
    return list(groups.values())


TOOL_LABELS = {"get_alerts": "Checking which alerts are firing", "get_metrics": "Reading current metrics",
               "get_logs": "Reading recent logs", "get_deploys": "Checking recent deploys"}


def summarize_result(name, raw):
    """One plain-English line describing a tool result, for the live timeline."""
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "error" in data:
            return f"Tool error: {data['error']}"
        if name == "get_alerts":
            return ("; ".join(f"{a['alert']} ({a['severity']}): {a['summary']}" for a in data)
                    or "No alerts firing")
        if name == "get_metrics":
            return (f"{data['requests_per_sec']} req/s, {data['error_rate_pct']}% errors, "
                    f"p95 {data['p95_latency_ms']} ms, memory {data['memory_mb']} MB, running {data['version']}")
        if name == "get_logs":
            top = sorted(data, key=lambda g: -g["count"])[:3]
            return f"{len(data)} distinct messages. Most frequent: " + "; ".join(
                f"{g['msg'][:90]} (x{g['count']})" for g in top) if data else "No log entries"
        if name == "get_deploys":
            return " -> ".join(f"{d['version']} ({d['note']})" for d in data) or "No deploys recorded"
    except Exception:
        pass
    return raw[:200]


def run_tool(name, args):
    """Execute one read-only tool. Returns a JSON string for the model."""
    try:
        if name == "get_alerts":
            return json.dumps(fetch("/api/alerts"))
        if name == "get_metrics":
            return json.dumps(fetch("/api/metrics"))
        if name == "get_logs":
            n = max(1, min(int(args.get("n", 30)), 100))
            return json.dumps(summarize_logs(fetch(f"/api/logs?n={n}")))
        if name == "get_deploys":
            return json.dumps(fetch("/api/deploys"))
        return json.dumps({"error": f"unknown tool {name}"})
    except Exception as e:  # tool failures go back to the model, not a crash
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def run_triage(client, verbose=True, model=None, emit=no_report):
    """Run one investigation. Returns the diagnosis dict, or None if it never concluded.

    `emit(kind, title, detail, **extra)` receives every step for the live timeline.
    """
    model = model or MODEL
    messages = [{"role": "user", "content": "An alert may be firing on the shop service. Investigate and diagnose."}]
    for turn in range(MAX_TURNS):
        emit("thinking", "Thinking about what to check next..." if turn else "Planning the investigation...")
        resp = client.messages.create(model=model, max_tokens=1500, system=SYSTEM_PROMPT,
                                      tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "text" and block.text.strip():
                if verbose:
                    print(f"  [agent] {block.text.strip()}")
                emit("thought", "Agent's reasoning", block.text.strip()[:700])
            if block.type != "tool_use":
                continue
            if block.name == "submit_diagnosis":
                if valid_diagnosis(block.input):
                    return block.input
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps({"error": "invalid diagnosis; resubmit with root_cause, "
                                                       "evidence[], proposed_action{type,detail}, confidence 0-1"})})
                continue
            if verbose:
                print(f"  [tool]  {block.name}({json.dumps(block.input)})")
            emit("tool_call", TOOL_LABELS.get(block.name, block.name),
                 json.dumps(block.input) if block.input else "", tool=block.name)
            raw = run_tool(block.name, block.input)
            emit("tool_result", "Found", summarize_result(block.name, raw))
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": raw})
        if not results:  # model answered in text: accept a well-formed JSON diagnosis, else give up
            return extract_diagnosis(" ".join(b.text for b in resp.content if b.type == "text"))
        messages.append({"role": "user", "content": results})
    return None


def report(diagnosis):
    a = diagnosis["proposed_action"]
    print("\n=== DIAGNOSIS ===")
    print(f"Root cause : {diagnosis['root_cause']}")
    print("Evidence   :")
    for e in diagnosis["evidence"]:
        print(f"  - {e}")
    print(f"Proposed   : {a['type']} - {a['detail']}")
    print(f"Confidence : {diagnosis['confidence']:.0%}\n")
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **diagnosis}) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true", help="poll for alerts every 10 seconds")
    parser.add_argument("--backend", choices=["anthropic", "ollama"],
                        help="LLM backend (default: anthropic if ANTHROPIC_API_KEY is set, else ollama)")
    parser.add_argument("--dry-run", action="store_true", help="ask for approval but never execute")
    parser.add_argument("--browser-approval", action="store_true",
                        help="approve or deny actions with buttons on the web page instead of the terminal")
    parser.add_argument("--diagnose-only", action="store_true", help="skip the action phase entirely")
    args = parser.parse_args()
    backend = args.backend or ("anthropic" if os.getenv("ANTHROPIC_API_KEY") else "ollama")
    if backend == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            sys.exit("ANTHROPIC_API_KEY is not set. Use --backend ollama for a free local model.")
        import anthropic
        client, model = anthropic.Anthropic(), MODEL
    else:
        import ollama_client
        client, model = ollama_client.OllamaClient(), os.getenv("AGENT_MODEL", ollama_client.DEFAULT_MODEL)
    print(f"Backend: {backend} ({model})")
    emit = Reporter()
    approve = guardrails.make_web_approver() if args.browser_approval else guardrails.cli_approve
    if args.browser_approval:
        print(f"Approvals happen in the browser: open {os.getenv('SHOP_URL', 'http://localhost:8090')}")

    while True:
        alerts = fetch("/api/alerts")
        if alerts:
            print(f"{len(alerts)} alert(s) firing: {', '.join(a['alert'] for a in alerts)}. Investigating...")
            emit("run_started", f"Alert detected: {', '.join(a['alert'] for a in alerts)}",
                 "; ".join(a["summary"] for a in alerts), tone="bad")
            diagnosis = run_triage(client, model=model, emit=emit)
            if diagnosis:
                report(diagnosis)
                act = diagnosis["proposed_action"]
                emit("diagnosis", "Diagnosis ready",
                     f"Proposed action: {act['type']}"
                     + (f" to {act['target_version']}" if act.get("target_version") else "")
                     + f" ({act['detail']}). Confidence {diagnosis['confidence']:.0%}.",
                     root_cause=diagnosis["root_cause"], evidence=diagnosis["evidence"],
                     confidence=diagnosis["confidence"], action=act["type"],
                     target_version=act.get("target_version", ""))
                if args.diagnose_only:
                    emit("outcome", "Diagnosis only: no action taken", tone="warn")
                else:
                    guardrails.handle(diagnosis, approve=approve, dry_run=args.dry_run, emit=emit)
            else:
                print("Agent did not reach a diagnosis; escalate to a human.")
                emit("outcome", "No diagnosis reached: escalating to a human", tone="bad")
            if args.watch:
                time.sleep(60)  # avoid re-triaging the same incident immediately
        elif not args.watch:
            print("No alerts firing. Nothing to triage.")
        if not args.watch:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
