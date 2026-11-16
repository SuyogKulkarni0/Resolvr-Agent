"""
Instant, read-only answers for the dashboard's chat box.

A question is matched to an intent by keywords and answered from live data (metrics, services,
processes, logs, deploys). There is no model call, so answers appear immediately even when the
local LLM takes minutes per incident. It can only read: it never changes anything.

`answer(question, ctx)` returns {"intent", "title", "rows": [{"s": ok|warn|bad|info, "t": text}]}.
`ctx` supplies the data as callables: metrics, alerts, processes, services, system, logs(n), deploys.
"""
import re

HELP_EXAMPLES = ["how are the services?", "how is the database?", "memory usage", "cpu usage",
                 "top processes", "any alerts?", "latency and errors", "recent errors in the logs", "last deploy"]

# order matters: the first pattern that matches wins
INTENTS = [
    ("help", r"\b(help|what can you|hello|hi|hey)\b"),
    ("database", r"\b(database|db|postgres|sql|pool)\b"),
    ("memory", r"\b(memory|mem|ram|leak)\b"),
    ("cpu", r"\b(cpu|load|processor)\b"),
    ("processes", r"\b(process|processes|top|hog|ps)\b"),
    ("alerts", r"\b(alert|alerts|firing|incident)\b"),
    ("inventory", r"\b(inventory|dependency|dependencies|upstream)\b"),
    ("logs", r"\b(logs?)\b"),
    ("metrics", r"\b(latency|p95|slow|response|errors? rate|error|errors|requests?|traffic|throughput|rps)\b"),
    ("deploys", r"\b(deploys?|deployed|version|release|rollback)\b"),
    ("services", r"\b(services?|status|health|healthy|overview|summary|everything|all|up|down)\b|how is|how's|how are"),
]
GLYPH = {"up": "ok", "degraded": "warn", "down": "bad"}


def row(s, t):
    return {"s": s, "t": t}


def level(value, warn, bad):
    return "bad" if value > bad else "warn" if value > warn else "ok"


def intent_of(question):
    q = question.lower()
    for name, pattern in INTENTS:
        if re.search(pattern, q):
            return name
    return "unknown"


def _services(ctx):
    rows = [row(GLYPH.get(s["status"], "info"), f"{s['name']:<20} {s['status']:<9} {s['detail']}") for s in ctx["services"]()]
    firing = ctx["alerts"]()
    rows.append(row("bad" if firing else "ok", f"overall: {len(firing)} alert(s) firing: " + ", ".join(a["alert"] for a in firing)
                    if firing else "overall: healthy, no alerts firing"))
    return "Service status", rows


def _database(ctx):
    db = next((s for s in ctx["services"]() if s["kind"] == "database"), None)
    m = ctx["metrics"]()
    rows = [row(GLYPH.get(db["status"], "info"), f"{db['name']}  {db['status']}  {db['detail']}")] if db else []
    if db and db["status"] != "up":
        rows.append(row("bad", f"5xx error rate is {m['error_rate_pct']}% while the database is unhealthy"))
    else:
        rows.append(row("info", f"5xx error rate {m['error_rate_pct']}%, no database errors in the current window"))
    return "Database", rows


def _memory(ctx):
    m, procs = ctx["metrics"](), sorted(ctx["processes"](), key=lambda p: -p["mem_mb"])
    total = sum(p["mem_mb"] for p in procs) or 1
    rows = [row(level(m["memory_mb"], 100, 150), f"{m['memory_mb']} MB in use · alert above 150 MB")]
    for p in procs[:3]:
        rows.append(row("info", f"{p['name']:<18} pid {p['pid']:<5} {p['mem_mb']:>6} MB  ({100 * p['mem_mb'] / total:.0f}% of total)"))
    top = procs[0]
    if m["memory_mb"] > 150:
        rows.append(row("bad", f"HighMemory territory: largest consumer is {top['name']} (pid {top['pid']})"))
    return "Memory", rows


def _cpu(ctx):
    sysm, procs = ctx["system"](), sorted(ctx["processes"](), key=lambda p: -p["cpu_pct"])
    rows = [row(level(sysm["cpu_pct"], 60, 85), f"host CPU {sysm['cpu_pct']}% across {sysm['cores']} cores")]
    for p in procs[:3]:
        rows.append(row("bad" if p["cpu_pct"] > 50 else "info", f"{p['name']:<18} pid {p['pid']:<5} {p['cpu_pct']:>5}% of one core"))
    return "CPU", rows


def _processes(ctx):
    procs = sorted(ctx["processes"](), key=lambda p: -p["mem_mb"])[:5]
    return "Top processes by memory", [
        row("bad" if p["cpu_pct"] > 50 and p["mem_mb"] > 60 else "info",
            f"{p['pid']:<5} {p['user']:<8} {p['cpu_pct']:>5}% cpu  {p['mem_mb']:>6} MB  {p['name']}") for p in procs]


def _alerts(ctx):
    firing = ctx["alerts"]()
    if not firing:
        return "Alerts", [row("ok", "no alerts firing (3 rules evaluated every second)")]
    return "Alerts", [row("bad" if a["severity"] == "critical" else "warn", f"{a['alert']} ({a['severity']}): {a['summary']}") for a in firing]


def _inventory(ctx):
    inv = next((s for s in ctx["services"]() if s["name"] == "inventory-service"), None)
    return "Inventory service", [row(GLYPH.get(inv["status"], "info"), f"{inv['name']}  {inv['status']}  {inv['detail']}")] if inv else [row("info", "not monitored")]


def _metrics(ctx):
    m = ctx["metrics"]()
    return "Traffic, errors and latency", [
        row("info", f"{m['requests_per_sec']} requests/sec"),
        row(level(m["error_rate_pct"], 2, 10), f"{m['error_rate_pct']}% 5xx errors · alert above 10%"),
        row(level(m["p95_latency_ms"], 400, 1000), f"p95 latency {m['p95_latency_ms']} ms · alert above 1000 ms"),
        row("info", f"running {m['version']}")]


def _logs(ctx):
    entries = [e for e in ctx["logs"](60) if e["level"] in ("ERROR", "WARN")][-6:]
    if not entries:
        return "Recent warnings and errors", [row("ok", "no warnings or errors in the recent log")]
    return "Recent warnings and errors", [row("bad" if e["level"] == "ERROR" else "warn", f"{e['ts'][11:19]} {e['level']}: {e['msg'][:110]}") for e in entries]


def _deploys(ctx):
    d = ctx["deploys"]()[-3:]
    return "Recent deploys", [row("info", f"{x['ts'][11:19]} {x['version']}  ({x['note']})") for x in d] or [row("info", "no deploys recorded")]


def _help(ctx):
    return "What I can answer", [row("info", "I read live data and never change anything. Try:")] + [row("info", f"  {e}") for e in HELP_EXAMPLES]


def _unknown(ctx):
    return "Not sure what you mean", [row("warn", "I answer from live data only, so I match topics, not free-form questions.")] + [
        row("info", f"  {e}") for e in HELP_EXAMPLES[:6]]


HANDLERS = {"help": _help, "database": _database, "memory": _memory, "cpu": _cpu, "processes": _processes,
            "alerts": _alerts, "inventory": _inventory, "metrics": _metrics, "logs": _logs,
            "deploys": _deploys, "services": _services, "unknown": _unknown}


def answer(question, ctx):
    intent = intent_of(question or "")
    title, rows = HANDLERS[intent](ctx)
    return {"intent": intent, "title": title, "rows": rows}
