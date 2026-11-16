#!/usr/bin/env python3
"""
One-command demo launcher (Windows, macOS, Linux; Python standard library only).

    python run_demo.py            # start the shop service and open the dashboard
    python run_demo.py --agent    # ...and also start Resolvr in watch mode

With --agent the agent picks its brain automatically: the Claude API if ANTHROPIC_API_KEY is
set, otherwise a local Ollama model if one is running, otherwise it is skipped with a hint.
Approvals are given with the Approve / Deny buttons on the dashboard. Press Ctrl+C to stop.
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))


def reachable(url, timeout=1.5):
    try:
        urllib.request.urlopen(url, timeout=timeout).read()
        return True
    except Exception:
        return False


def pick_backend():
    """Return 'anthropic', 'ollama' or None, matching how agent.py chooses a backend."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    if reachable(os.getenv("OLLAMA_URL", "http://localhost:11434") + "/api/tags"):
        return "ollama"
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8090, help="port for the dashboard (default 8090)")
    p.add_argument("--agent", action="store_true", help="also start Resolvr (watch mode, approvals in the browser)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    args = p.parse_args()

    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 or newer is required.")
    url = f"http://localhost:{args.port}"            # what people open in the browser
    probe = f"http://127.0.0.1:{args.port}"          # what we poll: skips slow IPv6 'localhost' lookups on some systems
    if reachable(probe + "/health"):
        sys.exit(f"Something is already running at {url}. Stop it, or pick another port with --port.")

    env = {**os.environ, "PORT": str(args.port), "SHOP_URL": probe, "PYTHONUNBUFFERED": "1"}
    procs = [subprocess.Popen([sys.executable, os.path.join(ROOT, "demo_app", "app.py")], env=env)]
    try:
        for _ in range(40):                                   # wait up to ~10 s for the service
            if reachable(probe + "/health"):
                break
            if procs[0].poll() is not None:
                sys.exit("The demo service exited unexpectedly. See the error above.")
            time.sleep(0.25)
        else:
            sys.exit("The demo service did not start in time.")
        print(f"\nDashboard: {url}")

        if args.agent:
            backend = pick_backend()
            if backend:
                print(f"Starting Resolvr ({backend}). Approve or deny fixes in the browser.")
                procs.append(subprocess.Popen(
                    [sys.executable, os.path.join(ROOT, "agent", "agent.py"), "--watch", "--browser-approval", "--backend", backend],
                    env=env))
            else:
                print("No model available for the agent, so it was not started.\n"
                      "  - Claude API: set ANTHROPIC_API_KEY and re-run, or\n"
                      "  - Local model: install Ollama (https://ollama.com), run `ollama pull qwen2.5:7b`, and re-run.")
        else:
            print("Tip: add --agent to also start Resolvr.")

        if not args.no_browser:
            webbrowser.open(url)
        print("Click a button under 'Failure injection' to break the service. Press Ctrl+C to stop.\n")
        while all(pr.poll() is None for pr in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        for pr in procs:
            if pr.poll() is None:
                pr.terminate()
        for pr in procs:
            try:
                pr.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pr.kill()


if __name__ == "__main__":
    main()
