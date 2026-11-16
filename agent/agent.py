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
Your action is only a proposal: a policy layer and a human approve it before anything runs. Use escalate_to_human when evidence is weak or conflicting.
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


def run_tool(name, args):
    """Execute one read-only tool. Returns a JSON string for the model."""
    try:
        if name == "get_alerts":
            return json.dumps(fetch("/api/alerts"))
        if name == "get_metrics":
            return json.dumps(fetch("/api/metrics"))
        if name == "get_logs":
            n = max(1, min(int(args.get("n", 30)), 100))
            return json.dumps(fetch(f"/api/logs?n={n}"))
        if name == "get_deploys":
            return json.dumps(fetch("/api/deploys"))
        return json.dumps({"error": f"unknown tool {name}"})
    except Exception as e:  # tool failures go back to the model, not a crash
        return json.dumps({"error": f"{type(e).__name__}: {e}"})


def run_triage(client, verbose=True):
    """Run one investigation. Returns the diagnosis dict, or None if it never concluded."""
    messages = [{"role": "user", "content": "An alert may be firing on the shop service. Investigate and diagnose."}]
    for turn in range(MAX_TURNS):
        resp = client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
                                      tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type == "text" and verbose and block.text.strip():
                print(f"  [agent] {block.text.strip()}")
            if block.type != "tool_use":
                continue
            if block.name == "submit_diagnosis":
                return block.input
            if verbose:
                print(f"  [tool]  {block.name}({json.dumps(block.input)})")
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": run_tool(block.name, block.input)})
        if not results:  # model stopped without a diagnosis
            return None
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
    parser.add_argument("--dry-run", action="store_true", help="ask for approval but never execute")
    parser.add_argument("--diagnose-only", action="store_true", help="skip the action phase entirely")
    args = parser.parse_args()
    if not os.getenv("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set.")
    import anthropic
    client = anthropic.Anthropic()

    while True:
        alerts = fetch("/api/alerts")
        if alerts:
            print(f"{len(alerts)} alert(s) firing: {', '.join(a['alert'] for a in alerts)}. Investigating...")
            diagnosis = run_triage(client)
            if diagnosis:
                report(diagnosis)
                if not args.diagnose_only:
                    guardrails.handle(diagnosis, dry_run=args.dry_run)
            else:
                print("Agent did not reach a diagnosis; escalate to a human.")
            if args.watch:
                time.sleep(60)  # avoid re-triaging the same incident immediately
        elif not args.watch:
            print("No alerts firing. Nothing to triage.")
        if not args.watch:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
