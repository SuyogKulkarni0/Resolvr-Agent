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

PORT = int(os.getenv("PORT", "8080"))
BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.getenv("LOG_DIR", os.path.join(BASE, "..", "data"))
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "app.log")
DEPLOY_FILE = os.path.join(LOG_DIR, "deploys.jsonl")

FAULTS = {"none", "leak", "errors", "slow", "inventory_down"}
VERSIONS = {"v1.0.0", "v1.1.0"}
BASELINE_MB = 35.0  # rough idle footprint of the interpreter + app

state = {"fault": "none", "version": "v1.0.0", "loadgen": True}
lock = threading.Lock()
requests_window = deque()          # (timestamp, status, latency_ms)
log_buffer = deque(maxlen=500)
leak = []

tracemalloc.start()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(level, message, **fields):
    entry = {"ts": now(), "level": level, "service": "shop",
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


def memory_mb():
    return round(BASELINE_MB + tracemalloc.get_traced_memory()[0] / 1e6, 1)


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


ALERT_RULES = [
    ("HighErrorRate", "critical", lambda m: m["error_rate_pct"] > 10,
     lambda m: f"5xx error rate is {m['error_rate_pct']}% (threshold 10%)"),
    ("HighLatencyP95", "warning", lambda m: m["p95_latency_ms"] > 1000,
     lambda m: f"p95 latency is {m['p95_latency_ms']} ms (threshold 1000 ms)"),
    ("HighMemory", "warning", lambda m: m["memory_mb"] > 150,
     lambda m: f"memory is {m['memory_mb']} MB (threshold 150 MB)"),
]


def alerts():
    m = metrics()
    return [{"alert": n, "severity": s, "summary": msg(m), "since": m["ts"]}
            for n, s, cond, msg in ALERT_RULES if m["window_requests"] and cond(m)]


def set_fault(mode):
    with lock:
        state["fault"] = mode
        if mode != "leak":
            leak.clear()
    log("WARN", f"fault mode set to '{mode}' (simulated)", event="fault_change")


def set_version(version, note="deploy"):
    with lock:
        state["version"] = version
    record_deploy(version, note)
    log("INFO", f"deployed {version}", event="deploy")


def reset():
    with lock:
        leak.clear()
        state["fault"] = "none"
        requests_window.clear()
    set_version("v1.0.0", "reset to healthy baseline")
    log("INFO", "reset to healthy state", event="reset")


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
        elif path == "/admin/loadgen":
            state["loadgen"] = bool(body.get("enabled", True))
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
    print(f"Shop demo running on http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
