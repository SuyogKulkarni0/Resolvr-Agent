"""
Demo "Shop" service for the Triage Agent project.

Zero dependencies (Python standard library only). It simulates a small production
service that you can break on purpose, so a monitoring agent has something real to
investigate:

  * GET  /                 live control panel (the demo UI)
  * GET  /orders           the "business" endpoint
  * GET  /health           liveness check
  * GET  /api/metrics      rolling metrics (req/s, error rate, p95 latency, memory)
  * GET  /api/alerts       alert rules evaluated against the metrics
  * GET  /api/processes    simulated process table (like `ps aux --sort=-%mem`)
  * GET  /api/services     health of shop-api, database, inventory-service and helpers
  * GET  /api/system       host CPU and memory
  * POST /api/chat         {"message": "how is the database?"}  read-only answer from live data
  * POST /admin/kill       {"name": "report-export"}   terminate the runaway process
  * POST /admin/restart    {"service": "shop"}
  * GET  /api/logs?n=50    recent structured logs
  * GET  /api/deploys      deploy history
  * POST /admin/fault      {"mode": "none|leak|errors|slow|inventory_down"}
  * POST /admin/deploy     {"version": "v1.0.0|v1.1.0"}   (v1.1.0 ships a bug)
  * POST /admin/reset      back to healthy v1.0.0
"""
import json
import os
import random
import threading
import time
import tracemalloc
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import chat

PORT = int(os.getenv("PORT", "8080"))
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.getenv("LOG_DIR", os.path.join(BASE, "..", "data"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")
DEPLOY_FILE = os.path.join(LOG_DIR, "deploys.jsonl")

FAULTS = {"none", "leak", "errors", "slow", "inventory_down", "rogue_process"}
VERSIONS = {"v1.0.0", "v1.1.0"}
BASELINE_MB = 35.0  # idle footprint: shop-api 20 + log-shipper 9 + metrics-exporter 6

# Simulated host processes. shop-api is the real server; the two helpers are fixed background
# processes. A "rogue_process" fault adds report-export, a runaway job that eats memory and CPU.
BACKGROUND = [("log-shipper", "root", 1187, 9.0, 1.2), ("metrics-exporter", "monitor", 1204, 6.0, 0.8)]
ROGUE = {"name": "report-export", "user": "batch", "pid": 4121, "cmd": "/opt/batch/report-export --full --no-limit"}
ROGUE_START_MB, ROGUE_GROWTH_MB_S, ROGUE_CAP_MB = 20.0, 5.0, 220.0
rogue = {"started": None}           # started = epoch seconds while the runaway process is alive

state = {"fault": "none", "version": "v1.0.0", "loadgen": True}
lock = threading.Lock()
requests_window = deque()          # (timestamp, status, latency_ms)
log_buffer = deque(maxlen=500)
leak = []

# Live agent activity, shown on the page as a timeline. Events are posted by the agent.
agent_events = deque(maxlen=500)
event_seq = [0]
approval = {"status": "none", "proposal": None}  # none | pending | approved | denied

tracemalloc.start()

# Alert bookkeeping and the live "checks" stream: what the monitor measured and when an alert
# fired or resolved. Together they let anyone confirm detection and recovery as it happens.
alert_state = {}                    # rule name -> {since, peak, severity, key, limit, unit} while firing
alert_history = deque(maxlen=20)    # resolved alerts, newest first
last_fail = {}                      # rule name -> last time it was failing (to show PASS lines after recovery)
check_log = deque(maxlen=400)
check_seq = [0]


def clip(value, limit=2000):
    return value[:limit] if isinstance(value, str) else value


def log_check(kind, subject, detail, verdict=""):
    """Append one line to the live checks stream (CHECK, ALERT, CHANGE, AGENT, ACTION)."""
    with lock:
        check_seq[0] += 1
        check_log.append({"id": check_seq[0], "ts": now(), "type": kind, "subject": subject,
                          "detail": detail, "verdict": verdict})


def mirror_agent_event(e):
    """Show the agent's key steps in the checks stream, so the fix sits between the failing and passing checks."""
    kind = e.get("kind")
    if kind == "run_started":
        log_check("AGENT", "triage-agent", "alert received, investigating")
    elif kind == "diagnosis":
        target = f" to {e['target_version']}" if e.get("target_version") else (f" {e['target']}" if e.get("target") else "")
        log_check("AGENT", "triage-agent", f"proposes {e.get('action', 'action')}{target}", "PROPOSED")
    elif kind == "approval":
        log_check("AGENT", "operator", str(e.get("title", "")).lower().replace("human ", ""),
                  "APPROVED" if "approved" in str(e.get("title", "")) else "DENIED")
    elif kind == "action":
        log_check("ACTION", "triage-agent", str(e.get("title", "")).replace("Executed: ", "") + " applied", "DONE")
    elif kind == "verification":
        log_check("AGENT", "triage-agent", e.get("title", "verification"), "PASS" if e.get("recovered") else "FAIL")
    elif kind == "outcome":
        log_check("AGENT", "triage-agent", e.get("title", ""), "")


def add_agent_event(body):
    """Store one event from the agent. Unknown fields are kept (e.g. diagnosis data)."""
    with lock:
        event_seq[0] += 1
        event = {k: clip(v) for k, v in body.items() if k not in ("id", "ts")}
        event.update(id=event_seq[0], ts=now())
        agent_events.append(event)
    mirror_agent_event(event)
    return event


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(level, message, **fields):
    entry = {"ts": now(), "level": level, "service": "shop", "host": "shop-01", "pid": os.getpid(),
             "version": state["version"], "msg": message, **fields}
    line = json.dumps(entry)
    with lock:
        log_buffer.append(entry)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def record_deploy(version, note):
    entry = {"ts": now(), "version": version, "note": note}
    with lock:
        with open(DEPLOY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


# ------------------------------------------------------------------ business logic
class AppError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def process_order():
    """Simulate handling one order. Returns (status, latency_ms)."""
    start = time.time()
    status = 200
    try:
        fault, version = state["fault"], state["version"]
        if fault == "slow":
            time.sleep(random.uniform(1.2, 2.5))
        if fault == "leak" and len(leak) < 300:
            leak.append(bytearray(b"x" * 1024 * 1024))  # 1 MB retained forever
        if fault == "inventory_down":
            raise AppError(502, "inventory-service unreachable: connection refused (inventory:8081)")
        if fault == "errors" and random.random() < 0.5:
            raise AppError(500, "database connection pool exhausted (max=10, waiting=37)")
        if version == "v1.1.0" and random.random() < 0.4:
            raise AppError(500, "KeyError: 'discount_code' at checkout.apply_discount (checkout.py:88)")
        time.sleep(random.uniform(0.01, 0.06))  # normal work
    except AppError as e:
        status = e.status
        log("ERROR", str(e), path="/orders", status=status)
    latency = (time.time() - start) * 1000
    with lock:
        requests_window.append((time.time(), status, latency))
    return status, latency


def rogue_mem():
    started = rogue["started"]
    return 0.0 if started is None else min(ROGUE_CAP_MB, ROGUE_START_MB + ROGUE_GROWTH_MB_S * (time.time() - started))


def memory_mb():
    return round(BASELINE_MB + tracemalloc.get_traced_memory()[0] / 1e6 + rogue_mem(), 1)


def processes():
    """What `ps aux --sort=-%mem` would show: the simulated host processes, biggest memory first."""
    rows = [{"pid": os.getpid(), "name": "shop-api", "user": "shop", "cmd": "python demo_app/app.py",
             "mem_mb": round(20.0 + tracemalloc.get_traced_memory()[0] / 1e6, 1), "cpu_pct": round(random.uniform(4, 9), 1)}]
    for name, user, pid, mem, cpu in BACKGROUND:
        rows.append({"pid": pid, "name": name, "user": user, "cmd": f"/usr/bin/{name}", "mem_mb": mem,
                     "cpu_pct": round(cpu + random.uniform(0, 0.4), 1)})
    if rogue["started"] is not None:
        rows.append({"pid": ROGUE["pid"], "name": ROGUE["name"], "user": ROGUE["user"], "cmd": ROGUE["cmd"],
                     "mem_mb": round(rogue_mem(), 1), "cpu_pct": round(random.uniform(88, 99), 1)})
    return sorted(rows, key=lambda r: -r["mem_mb"])


def metrics():
    cutoff30, cutoff10 = time.time() - 30, time.time() - 10
    with lock:
        while requests_window and requests_window[0][0] < cutoff30:
            requests_window.popleft()
        recent = list(requests_window)
    total = len(recent)
    errors = sum(1 for _, s, _ in recent if s >= 500)
    lats = sorted(l for _, _, l in recent)
    p95 = lats[int(0.95 * (len(lats) - 1))] if lats else 0.0
    rps = sum(1 for t, _, _ in recent if t >= cutoff10) / 10.0
    return {
        "ts": now(), "version": state["version"], "fault": state["fault"],
        "loadgen": state["loadgen"], "requests_per_sec": round(rps, 1),
        "error_rate_pct": round(100.0 * errors / total, 1) if total else 0.0,
        "p95_latency_ms": round(p95), "memory_mb": memory_mb(),
        "window_requests": total,
    }


# (name, severity, metric key, limit, unit, label for the summary text)
ALERT_RULES = [
    ("HighErrorRate", "critical", "error_rate_pct", 10, "%", "5xx error rate"),
    ("HighLatencyP95", "warning", "p95_latency_ms", 1000, " ms", "p95 latency"),
    ("HighMemory", "warning", "memory_mb", 150, " MB", "memory"),
]


def alerts():
    """Currently firing alerts, with how long each has been firing."""
    m = metrics()
    out = []
    for name, severity, key, limit, unit, label in ALERT_RULES:
        if m["window_requests"] and m[key] > limit:
            with lock:
                since = alert_state.get(name, {}).get("since", time.time())
            out.append({"alert": name, "severity": severity, "key": key, "value": m[key], "limit": limit,
                        "unit": unit, "since_epoch": since, "since": m["ts"],
                        "summary": f"{label} is {m[key]}{unit} (threshold {limit}{unit})"})
    return out


def set_fault(mode):
    with lock:
        state["fault"] = mode
        if mode != "leak":
            leak.clear()
        rogue["started"] = time.time() if mode == "rogue_process" else None
    # Deliberately vague: naming the mode here would hand the answer to whoever reads the logs.
    log("WARN", "runtime configuration changed", event="config_change")
    log_check("CHANGE", "config", "runtime configuration changed")


def set_version(version, note="deploy"):
    with lock:
        state["version"] = version
    record_deploy(version, note)
    log("INFO", f"deployed {version}", event="deploy")
    log_check("CHANGE", "deploy", f"{version} deployed ({note})")


CORES = 2


def system():
    """Host-level numbers: CPU is the sum of per-process CPU spread over the cores."""
    procs = processes()
    return {"cpu_pct": round(min(100.0, sum(p["cpu_pct"] for p in procs) / CORES), 1), "cores": CORES,
            "memory_mb": memory_mb(), "processes": len(procs)}


def services():
    """Health of each component the shop depends on, derived from the current faults and metrics."""
    m, fault = metrics(), state["fault"]
    api = "down" if m["error_rate_pct"] > 50 else "degraded" if m["error_rate_pct"] > 10 or m["p95_latency_ms"] > 1000 else "up"
    out = [{"name": "shop-api", "kind": "service", "status": api,
            "detail": f"{m['requests_per_sec']} req/s, {m['error_rate_pct']}% errors, p95 {m['p95_latency_ms']} ms, {m['version']}"}]
    if fault == "errors":
        out.append({"name": "postgres (database)", "kind": "database", "status": "degraded",
                    "detail": "connection pool exhausted (max=10, waiting=37)"})
    else:
        out.append({"name": "postgres (database)", "kind": "database", "status": "up",
                    "detail": f"pool {random.randint(2, 5)}/10 connections, query {random.randint(3, 6)} ms"})
    out.append({"name": "inventory-service", "kind": "service", "status": "down" if fault == "inventory_down" else "up",
                "detail": "connection refused (inventory:8081)" if fault == "inventory_down" else f"reachable, {random.randint(9, 15)} ms"})
    for name, user, pid, mem, cpu in BACKGROUND:
        out.append({"name": name, "kind": "process", "status": "up", "detail": f"pid {pid}, {mem} MB"})
    if rogue["started"] is not None:
        out.append({"name": ROGUE["name"], "kind": "process", "status": "degraded",
                    "detail": f"pid {ROGUE['pid']}, {round(rogue_mem(), 1)} MB, runaway CPU"})
    return out


def read_logs(n):
    with lock:
        return list(log_buffer)[-n:]


def read_deploys():
    try:
        with open(DEPLOY_FILE, encoding="utf-8") as f:
            return [json.loads(l) for l in f][-20:]
    except FileNotFoundError:
        return []


def chat_answer(question):
    return chat.answer(question, {"metrics": metrics, "alerts": alerts, "processes": processes, "services": services,
                                  "system": system, "logs": read_logs, "deploys": read_deploys})


def kill_process(name, pid=None):
    """Terminate a simulated process. Only the runaway job can be killed; shop-api and the helpers cannot."""
    with lock:
        if name != ROGUE["name"] or rogue["started"] is None or pid not in (None, ROGUE["pid"]):
            return False
        rogue["started"] = None
        if state["fault"] == "rogue_process":
            state["fault"] = "none"
    log("WARN", f"process {name} (pid {ROGUE['pid']}) terminated by operator (SIGTERM)", event="process_killed")
    log_check("ACTION", name, f"pid {ROGUE['pid']} terminated (SIGTERM)", "DONE")
    return True


def restart_service(service):
    """Restart the shop service: frees leaked memory. (Simulated: the leak trigger is cleared too.)"""
    if service != "shop":
        return False
    with lock:
        leak.clear()
        if state["fault"] == "leak":
            state["fault"] = "none"
    log("INFO", "service shop restarted", event="restart")
    log_check("ACTION", "shop", "service restarted, memory released", "DONE")
    return True


def reset():
    """Return to a clean healthy state, including logs and deploy history.

    Clearing history matters: stale errors from a previous incident would otherwise show up
    as evidence in the next one and mislead whoever (or whatever) is investigating.
    """
    with lock:
        leak.clear()
        rogue["started"] = None
        state["fault"] = "none"
        requests_window.clear()
        log_buffer.clear()
        check_log.clear()
        alert_state.clear()
        alert_history.clear()
        last_fail.clear()
        agent_events.clear()
        approval.update(status="none", proposal=None)
        open(DEPLOY_FILE, "w").close()
    set_version("v1.0.0", "baseline")
    log("INFO", "reset to healthy state", event="reset")


def monitor():
    """The monitoring loop: evaluate every alert rule each second and record what it saw.

    - A rule going over its limit adds an ALERT FIRING line; coming back adds ALERT RESOLVED.
    - Every 5 s a failing rule adds a CHECK FAIL line, and for 15 s after recovery a CHECK PASS
      line every 3 s, so the log shows the metric above the limit, then the fix, then the metric back under it.
    - When everything is healthy it adds one summary line every 15 s.
    """
    tick, last_summary = 0, 0.0
    while True:
        m = metrics()
        if m["window_requests"]:
            now_s, produced = time.time(), False
            for name, severity, key, limit, unit, label in ALERT_RULES:
                value, firing = m[key], m[key] > limit
                with lock:
                    st = alert_state.get(name)
                if firing and not st:
                    with lock:
                        alert_state[name] = {"since": now_s, "peak": value, "severity": severity, "key": key, "limit": limit, "unit": unit}
                    log_check("ALERT", name, f"{label} {value}{unit} exceeds {limit}{unit}", "FIRING")
                elif firing:
                    st["peak"] = max(st["peak"], value)
                elif st:
                    secs = round(now_s - st["since"])
                    with lock:
                        alert_state.pop(name, None)
                        alert_history.appendleft({"alert": name, "severity": severity, "fired_at": datetime.fromtimestamp(st["since"], timezone.utc).isoformat(timespec="seconds"),
                                                  "resolved_at": now(), "duration_s": secs, "peak": st["peak"], "limit": limit, "unit": unit})
                    log_check("ALERT", name, f"{label} back to {value}{unit} after {secs}s", "RESOLVED")
                if firing:
                    last_fail[name] = now_s
                if firing and tick % 5 == 0:
                    log_check("CHECK", key, f"{value}{unit} > {limit}{unit}", "FAIL"); produced = True
                elif not firing and tick % 3 == 0 and now_s - last_fail.get(name, 0) < 15:
                    log_check("CHECK", key, f"{value}{unit} <= {limit}{unit}", "PASS"); produced = True
            if tick % 3 == 0 and not produced and now_s - last_summary >= 15:
                log_check("CHECK", "all rules", f"{len(ALERT_RULES)}/{len(ALERT_RULES)} healthy "
                          f"(errors {m['error_rate_pct']}%, p95 {m['p95_latency_ms']} ms, memory {m['memory_mb']} MB)", "PASS")
                last_summary = now_s
        tick += 1
        time.sleep(1)


def loadgen_worker():
    while True:
        if state["loadgen"]:
            process_order()
        time.sleep(0.8)


# ------------------------------------------------------------------ HTTP layer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default access log
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body).encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            with open(os.path.join(BASE, "index.html"), "rb") as f:
                return self.send(200, f.read(), "text/html; charset=utf-8")
        if url.path == "/orders":
            status, latency = process_order()
            return self.send(status, {"status": "ok" if status < 400 else "error",
                                      "latency_ms": round(latency)})
        if url.path == "/health":
            return self.send(200, {"status": "up", "version": state["version"]})
        if url.path == "/api/metrics":
            return self.send(200, metrics())
        if url.path == "/api/alerts":
            return self.send(200, alerts())
        if url.path == "/api/processes":
            return self.send(200, processes())
        if url.path == "/api/services":
            return self.send(200, services())
        if url.path == "/api/system":
            return self.send(200, system())
        if url.path == "/api/logs":
            n = int(parse_qs(url.query).get("n", ["50"])[0])
            with lock:
                return self.send(200, list(log_buffer)[-n:])
        if url.path == "/api/deploys":
            try:
                with open(DEPLOY_FILE, encoding="utf-8") as f:
                    return self.send(200, [json.loads(l) for l in f][-20:])
            except FileNotFoundError:
                return self.send(200, [])
        if url.path == "/api/checks":
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            with lock:
                return self.send(200, {"entries": [c for c in check_log if c["id"] > since][-200:], "latest": check_seq[0]})
        if url.path == "/api/alerts/resolved":
            with lock:
                return self.send(200, list(alert_history))
        if url.path == "/api/agent/events":
            with lock:
                return self.send(200, {"events": list(agent_events), "approval": dict(approval)})
        if url.path == "/api/agent/approval":
            with lock:
                return self.send(200, dict(approval))
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self.json_body()
        if path == "/admin/fault":
            mode = body.get("mode")
            if mode not in FAULTS:
                return self.send(400, {"error": f"mode must be one of {sorted(FAULTS)}"})
            set_fault(mode)
        elif path == "/admin/deploy":
            version = body.get("version")
            if version not in VERSIONS:
                return self.send(400, {"error": f"version must be one of {sorted(VERSIONS)}"})
            set_version(version, body.get("note", "manual deploy"))
        elif path == "/api/chat":  # read-only questions from the dashboard chat box
            return self.send(200, chat_answer(str(body.get("message", ""))[:300]))
        elif path == "/admin/kill":
            if not kill_process(body.get("name"), body.get("pid")):
                return self.send(409, {"error": "no such killable process"})
        elif path == "/admin/restart":
            if not restart_service(body.get("service")):
                return self.send(409, {"error": "unknown service"})
        elif path == "/admin/loadgen":
            state["loadgen"] = bool(body.get("enabled", True))
        elif path == "/api/agent/event":
            return self.send(200, add_agent_event(body))
        elif path == "/api/agent/approval/request":  # agent asks a human to approve a proposed action
            with lock:
                approval.update(status="pending", proposal=body.get("proposal"),
                                root_cause=clip(body.get("root_cause", "")), confidence=body.get("confidence"))
            return self.send(200, dict(approval))
        elif path == "/api/agent/approval/decision":  # the human clicks Approve or Deny in the page
            decision = body.get("decision")
            if decision not in ("approved", "denied"):
                return self.send(400, {"error": "decision must be approved or denied"})
            with lock:
                if approval["status"] != "pending":
                    return self.send(409, {"error": "no approval is pending"})
                approval["status"] = decision
            return self.send(200, dict(approval))
        elif path == "/admin/reset":
            reset()
        else:
            return self.send(404, {"error": "not found"})
        self.send(200, {"ok": True, "state": {k: state[k] for k in ("fault", "version", "loadgen")}})


def main():
    record_deploy("v1.0.0", "initial startup")
    log("INFO", "shop service starting", event="startup", port=PORT)
    for _ in range(4):
        threading.Thread(target=loadgen_worker, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    print(f"Shop demo running on http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
