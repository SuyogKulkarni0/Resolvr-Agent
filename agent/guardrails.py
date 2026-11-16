"""
Phase 3: guarded actions.

The model only *proposes* an action. This module decides whether it may run:

  1. Allowlist      only `rollback` (to a known-good version), `clear_fault`, `kill_process`
                    (named runaway jobs only) and `restart_service` (the shop only)
  2. Confidence     below MIN_CONFIDENCE the agent escalates instead of acting
  3. Rate limit     at most MAX_ACTIONS_PER_HOUR executed actions
  4. Human approval nothing runs without an explicit "y" (default is No)
  5. Verification   after acting, alerts are re-checked to see if it worked
  6. Audit log      every decision is appended to data/audit.jsonl
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

SHOP_URL = os.getenv("SHOP_URL", "http://localhost:8090")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
AUDIT_FILE = os.path.join(DATA_DIR, "audit.jsonl")

MIN_CONFIDENCE = 0.7
MAX_ACTIONS_PER_HOUR = 3
KNOWN_GOOD_VERSIONS = {"v1.0.0"}
ALLOWED_ACTIONS = {"rollback", "clear_fault", "kill_process", "restart_service"}
KILLABLE_PROCESSES = {"report-export"}   # the shop's own processes (shop-api, ...) are never on this list
RESTARTABLE_SERVICES = {"shop"}
VERIFY_TIMEOUT_S = 45
VERIFY_INTERVAL_S = 3


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP_URL + path, data=data, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def audit(event, **fields):
    os.makedirs(os.path.dirname(AUDIT_FILE), exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields}
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def actions_in_last_hour():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        with open(AUDIT_FILE, encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return 0
    return sum(1 for e in entries
               if e["event"] == "executed" and datetime.fromisoformat(e["ts"]) >= cutoff)


def check_policy(diagnosis):
    """Return (allowed, reason). Pure policy, no side effects."""
    action = diagnosis["proposed_action"]
    kind = action["type"]
    if kind in ("none", "escalate_to_human"):
        return False, f"agent chose '{kind}'"
    if kind not in ALLOWED_ACTIONS:
        return False, f"action '{kind}' is not on the allowlist {sorted(ALLOWED_ACTIONS)}"
    if diagnosis["confidence"] < MIN_CONFIDENCE:
        return False, f"confidence {diagnosis['confidence']:.0%} is below the {MIN_CONFIDENCE:.0%} minimum"
    if kind == "rollback" and action.get("target_version") not in KNOWN_GOOD_VERSIONS:
        return False, (f"rollback target '{action.get('target_version')}' is not a known-good "
                       f"version {sorted(KNOWN_GOOD_VERSIONS)}")
    if kind == "kill_process" and action.get("target") not in KILLABLE_PROCESSES:
        return False, (f"process '{action.get('target')}' is not on the kill allowlist "
                       f"{sorted(KILLABLE_PROCESSES)}")
    if kind == "restart_service" and action.get("target") not in RESTARTABLE_SERVICES:
        return False, (f"service '{action.get('target')}' is not on the restart allowlist "
                       f"{sorted(RESTARTABLE_SERVICES)}")
    if actions_in_last_hour() >= MAX_ACTIONS_PER_HOUR:
        return False, f"rate limit: {MAX_ACTIONS_PER_HOUR} actions already executed in the last hour"
    return True, "within policy"


def build_request(action):
    if action["type"] == "rollback":
        return "/admin/deploy", {"version": action["target_version"], "note": "automated rollback (approved)"}
    if action["type"] == "kill_process":
        return "/admin/kill", {"name": action["target"]}
    if action["type"] == "restart_service":
        return "/admin/restart", {"service": action["target"]}
    return "/admin/fault", {"mode": "none"}


def verify_recovery(timeout=VERIFY_TIMEOUT_S, interval=VERIFY_INTERVAL_S):
    """Poll alerts until they clear or the timeout passes. Returns (recovered, remaining_alerts)."""
    deadline = time.time() + timeout
    alerts = http("GET", "/api/alerts")
    while alerts and time.time() < deadline:
        time.sleep(interval)
        alerts = http("GET", "/api/alerts")
    return (not alerts), alerts


def calm(m):
    """True when every metric is comfortably inside its normal range (not just under the alert limit)."""
    return m.get("error_rate_pct", 0) <= 2 and m.get("p95_latency_ms", 0) <= 400 and m.get("memory_mb", 0) <= 100


def settle(timeout=40, interval=3):
    """Wait for the service's rolling windows to drain, then return the metrics.

    Alerts clear as soon as a metric dips under its limit, but a 30 s rolling window still holds
    some of the failure. Waiting until the numbers are calm makes the 'after' value show real
    recovery instead of 'just barely under the limit'.
    """
    deadline = time.time() + timeout
    m = http("GET", "/api/metrics")
    while not calm(m) and time.time() < deadline:
        time.sleep(interval)
        m = http("GET", "/api/metrics")
    return m


def cli_approve(diagnosis):
    a = diagnosis["proposed_action"]
    target = f" to {a['target_version']}" if a.get("target_version") else (f" {a['target']}" if a.get("target") else "")
    answer = input(f"Approve '{a['type']}{target}'? Confidence {diagnosis['confidence']:.0%}. [y/N]: ")
    return answer.strip().lower() in ("y", "yes")


def make_web_approver(timeout=600, interval=1.5):
    """Approval through buttons on the web page. Anything but an explicit Approve is a No."""
    def approve(diagnosis):
        http("POST", "/api/agent/approval/request", {
            "proposal": diagnosis["proposed_action"], "root_cause": diagnosis["root_cause"],
            "confidence": diagnosis["confidence"]})
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = http("GET", "/api/agent/approval")["status"]
            if status in ("approved", "denied"):
                return status == "approved"
            time.sleep(interval)
        try:
            http("POST", "/api/agent/approval/decision", {"decision": "denied"})
        except Exception:
            pass
        return False  # timed out: default is No
    return approve


def _quiet(*args, **kwargs):
    pass


def handle(diagnosis, approve=cli_approve, dry_run=False, verify=verify_recovery, say=print, emit=_quiet):
    """Apply policy, ask a human, execute, verify, audit. Returns the outcome name.

    `emit(kind, title, detail, **extra)` mirrors each step to the live timeline.
    """
    action = diagnosis["proposed_action"]
    audit("diagnosis", root_cause=diagnosis["root_cause"], proposed=action, confidence=diagnosis["confidence"])

    allowed, reason = check_policy(diagnosis)
    if not allowed:
        audit("escalated", reason=reason, proposed=action)
        say(f"No automatic action: {reason}. Escalating to a human.")
        emit("policy", "Policy check: blocked", reason, tone="warn")
        emit("outcome", "Escalated to a human: no automatic action taken", tone="warn")
        return "escalated"
    emit("policy", "Policy check: passed", "Allowlisted action, confidence above the floor, under the rate limit.",
         tone="good")

    emit("thinking", "Waiting for a human to approve or deny...")
    if not approve(diagnosis):
        audit("denied", proposed=action, by="human")
        say("Action denied. Nothing was changed.")
        emit("approval", "Human denied the action", "Nothing was changed.", tone="bad")
        emit("outcome", "Action denied: no changes made", tone="warn")
        return "denied"

    audit("approved", proposed=action, by="human")
    emit("approval", "Human approved the action", tone="good")
    if dry_run:
        audit("dry_run", proposed=action)
        say("Dry run: approved but not executed.")
        emit("outcome", "Dry run: approved but not executed", tone="warn")
        return "dry_run"

    path, body = build_request(action)
    try:
        response = http("POST", path, body)
    except Exception as e:
        audit("execution_failed", proposed=action, error=f"{type(e).__name__}: {e}")
        say(f"Execution failed: {e}. Escalating to a human.")
        emit("action", "Execution failed", f"{type(e).__name__}: {e}", tone="bad")
        emit("outcome", "Escalated to a human: execution failed", tone="bad")
        return "execution_failed"
    audit("executed", proposed=action, request={"path": path, "body": body}, response=response)
    say(f"Executed {action['type']}. Verifying recovery (up to {VERIFY_TIMEOUT_S}s)...")
    emit("action", f"Executed: {action['type']}"
         + (f" to {action['target_version']}" if action.get("target_version") else
            (f" {action['target']}" if action.get("target") else "")), tone="good")
    emit("thinking", f"Checking whether the alerts clear (up to {VERIFY_TIMEOUT_S}s)...")

    recovered, remaining = verify()
    try:
        after = settle()  # the "after" half of the before/after proof
    except Exception:
        after = None
    try:
        procs_after = http("GET", "/api/processes")[:5]
    except Exception:
        procs_after = None
    if recovered:
        audit("verified", recovered=True, metrics_after=after)
        say("Verified: alerts cleared. Incident resolved.")
        emit("verification", "Verified: alerts cleared", tone="good", recovered=True, metrics=after,
             processes=procs_after)
        emit("outcome", "Incident resolved", tone="good")
        return "resolved"
    audit("verification_failed", remaining=[a["alert"] for a in remaining], metrics_after=after)
    say(f"Alerts still firing: {[a['alert'] for a in remaining]}. Escalating to a human.")
    emit("verification", "Alerts are still firing", ", ".join(a["alert"] for a in remaining), tone="bad",
         recovered=False, metrics=after, processes=procs_after)
    emit("outcome", "Escalated to a human: the fix did not work", tone="bad")
    return "verification_failed"
