"""Guardrail tests: no network, audit log redirected to a temp file."""
import json
import os
import tempfile
import unittest
from unittest import mock

import guardrails


def diag(kind="rollback", confidence=0.9, target="v1.0.0"):
    action = {"type": kind, "detail": "test"}
    if target:
        action["target_version"] = target
    return {"root_cause": "bad deploy", "evidence": ["x"], "proposed_action": action, "confidence": confidence}


class GuardrailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(guardrails, "AUDIT_FILE", os.path.join(self.tmp.name, "audit.jsonl"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.said = []

    def run_handle(self, d, approve=lambda d: True, **kw):
        kw.setdefault("verify", lambda: (True, []))
        return guardrails.handle(d, approve=approve, say=self.said.append, **kw)

    def events(self):
        with open(guardrails.AUDIT_FILE, encoding="utf-8") as f:
            return [json.loads(l)["event"] for l in f]

    def test_approved_rollback_executes_and_verifies(self):
        with mock.patch.object(guardrails, "http", return_value={"ok": True}) as http:
            self.assertEqual(self.run_handle(diag()), "resolved")
        http.assert_any_call("POST", "/admin/deploy",
                             {"version": "v1.0.0", "note": "automated rollback (approved)"})
        self.assertEqual(self.events(), ["diagnosis", "approved", "executed", "verified"])

    def test_human_denial_changes_nothing(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag(), approve=lambda d: False), "denied")
        http.assert_not_called()
        self.assertEqual(self.events(), ["diagnosis", "denied"])

    def test_low_confidence_escalates_without_asking(self):
        asked = mock.Mock(return_value=True)
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag(confidence=0.5), approve=asked), "escalated")
        asked.assert_not_called()
        http.assert_not_called()

    def test_action_off_allowlist_is_blocked(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag("restart", target=None)), "escalated")
        http.assert_not_called()

    def test_rollback_to_unknown_version_is_blocked(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag(target="v0.0.1")), "escalated")
        http.assert_not_called()

    def test_rate_limit_blocks_after_three_actions(self):
        for _ in range(guardrails.MAX_ACTIONS_PER_HOUR):
            guardrails.audit("executed")
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag()), "escalated")
        http.assert_not_called()

    def test_failed_verification_escalates(self):
        with mock.patch.object(guardrails, "http", return_value={"ok": True}):
            outcome = self.run_handle(diag(), verify=lambda: (False, [{"alert": "HighErrorRate"}]))
        self.assertEqual(outcome, "verification_failed")
        self.assertEqual(self.events()[-1], "verification_failed")

    def test_dry_run_never_executes(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(diag(), dry_run=True), "dry_run")
        http.assert_not_called()

    def test_clear_fault_action(self):
        with mock.patch.object(guardrails, "http", return_value={"ok": True}) as http:
            self.assertEqual(self.run_handle(diag("clear_fault", target=None)), "resolved")
        http.assert_any_call("POST", "/admin/fault", {"mode": "none"})

    @staticmethod
    def action_diag(kind, target):
        return {"root_cause": "x", "evidence": ["y"], "confidence": 0.9,
                "proposed_action": {"type": kind, "detail": "d", "target": target}}

    def test_kill_runaway_process_executes(self):
        with mock.patch.object(guardrails, "http", return_value={"ok": True}) as http:
            self.assertEqual(self.run_handle(GuardrailTest.action_diag("kill_process", "report-export")), "resolved")
        http.assert_any_call("POST", "/admin/kill", {"name": "report-export"})

    def test_cannot_kill_the_shop_itself(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(GuardrailTest.action_diag("kill_process", "shop-api")), "escalated")
        http.assert_not_called()

    def test_kill_without_a_target_is_blocked(self):
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(GuardrailTest.action_diag("kill_process", None)), "escalated")
        http.assert_not_called()

    def test_restart_service_executes_only_for_shop(self):
        with mock.patch.object(guardrails, "http", return_value={"ok": True}) as http:
            self.assertEqual(self.run_handle(GuardrailTest.action_diag("restart_service", "shop")), "resolved")
        http.assert_any_call("POST", "/admin/restart", {"service": "shop"})
        with mock.patch.object(guardrails, "http") as http:
            self.assertEqual(self.run_handle(GuardrailTest.action_diag("restart_service", "database")), "escalated")
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
