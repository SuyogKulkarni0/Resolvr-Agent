"""
Live reporting: sends each step the agent takes to the demo app, so a person can watch the
investigation on the page as it happens.

Reporting is best-effort. If the page or network is unavailable the agent carries on; the
audit log (guardrails.py) remains the source of truth.
"""
import json
import os
import urllib.request

SHOP_URL = os.getenv("SHOP_URL", "http://localhost:8090")


class Reporter:
    def __init__(self, base_url=SHOP_URL, timeout=2):
        self.base_url, self.timeout = base_url, timeout

    def __call__(self, kind, title, detail="", **extra):
        body = {"kind": kind, "title": title, "detail": detail, **extra}
        req = urllib.request.Request(self.base_url + "/api/agent/event", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=self.timeout).read()
        except Exception:
            pass  # never let a display problem stop an investigation


def no_report(kind, title, detail="", **extra):
    """Default sink used when no reporter is supplied (tests, library use)."""
