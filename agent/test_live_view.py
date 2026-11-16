"""Tests for the live timeline plumbing: event summaries, reporting and web approval."""
import json
import os
import tempfile
import unittest
from unittest import mock

import agent
import guardrails
import reporter


class SummaryTest(unittest.TestCase):
    def test_summaries_are_plain_english(self):
        alerts = json.dumps([{"alert": "HighErrorRate", "severity": "critical", "summary": "5xx at 30%"}])
        self.assertIn("HighErrorRate (critical): 5xx at 30%", agent.summarize_result("get_alerts", alerts))
        self.assertEqual(agent.summarize_result("get_alerts", "[]"), "No alerts firing")
        m = json.dumps({"requests_per_sec": 4.8, "error_rate_pct": 32, "p95_latency_ms": 60,
                        "memory_mb": 36, "version": "v1.1.0"})
        self.assertIn("32% errors", agent.summarize_result("get_metrics", m))
        deploys = json.dumps([{"version": "v1.0.0", "note": "baseline"}, {"version": "v1.1.0", "note": "release"}])
        self.assertEqual(agent.summarize_result("get_deploys", deploys), "v1.0.0 (baseline) -> v1.1.0 (release)")

    def test_tool_errors_are_summarized(self):
        self.assertTrue(agent.summarize_result("get_logs", json.dumps({"error": "boom"})).startswith("Tool error"))


class ReporterTest(unittest.TestCase):
    def test_reporting_failure_never_raises(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            reporter.Reporter("http://localhost:1")("tool_call", "x")  # must not raise


class WebApprovalTest(unittest.TestCase):
    diagnosis = {"root_cause": "x", "evidence": [], "confidence": 0.9,
                 "proposed_action": {"type": "rollback", "detail": "d", "target_version": "v1.0.0"}}

    def run_with_statuses(self, statuses, **kw):
        calls = []

        def fake_http(method, path, body=None):
            calls.append((method, path))
            return {"status": statuses.pop(0)} if path == "/api/agent/approval" else {"ok": True}

        with mock.patch.object(guardrails, "http", side_effect=fake_http), mock.patch("time.sleep"):
            return guardrails.make_web_approver(**kw)(self.diagnosis), calls

    def test_approve_button(self):
        ok, calls = self.run_with_statuses(["pending", "approved"])
        self.assertTrue(ok)
        self.assertEqual(calls[0], ("POST", "/api/agent/approval/request"))

    def test_deny_button(self):
        self.assertFalse(self.run_with_statuses(["denied"])[0])

    def test_timeout_means_no(self):
        with mock.patch.object(guardrails, "http", return_value={"status": "pending"}), \
             mock.patch("time.sleep"), mock.patch("time.time", side_effect=[0, 1, 2, 999, 999]):
            self.assertFalse(guardrails.make_web_approver(timeout=5)(self.diagnosis))


class HandleEmitsTest(unittest.TestCase):
    def test_every_stage_is_reported(self):
        diag = {"root_cause": "x", "evidence": [], "confidence": 0.9,
                "proposed_action": {"type": "rollback", "detail": "d", "target_version": "v1.0.0"}}
        events = []
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(guardrails, "AUDIT_FILE", os.path.join(tmp, "audit.jsonl")), \
             mock.patch.object(guardrails, "http", return_value={"ok": True}):
            outcome = guardrails.handle(diag, approve=lambda d: True, verify=lambda: (True, []),
                                        say=lambda *_: None, emit=lambda kind, title, detail="", **x: events.append(kind))
        self.assertEqual(outcome, "resolved")
        self.assertEqual([k for k in events if k != "thinking"],
                         ["policy", "approval", "action", "verification", "outcome"])


if __name__ == "__main__":
    unittest.main()
