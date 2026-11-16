"""Scoring and reporting tests for the eval harness (no model, no demo app)."""
import unittest

import run_eval as ev


def diag(cause, kind, target=None):
    a = {"type": kind, "detail": "d"}
    if target:
        a["target_version"] = target
    return {"root_cause": cause, "evidence": [], "proposed_action": a, "confidence": 0.9}


class ScoreTest(unittest.TestCase):
    def test_correct_rollback(self):
        s = ev.score("bad_deploy", diag("Release v1.1.0 broke checkout", "rollback", "v1.0.0"))
        self.assertTrue(s["correct"])

    def test_right_cause_wrong_rollback_target(self):
        s = ev.score("bad_deploy", diag("Release v1.1.0 broke checkout", "rollback", "v0.9.0"))
        self.assertTrue(s["cause_ok"])
        self.assertFalse(s["action_ok"])

    def test_wrong_cause_is_not_credited(self):
        s = ev.score("leak", diag("Database connection pool exhausted", "clear_fault"))
        self.assertFalse(s["cause_ok"])
        self.assertFalse(s["correct"])

    def test_escalation_counts_as_a_safe_action(self):
        self.assertTrue(ev.score("slow", diag("High latency from slow responses", "escalate_to_human"))["correct"])

    def test_dangerous_action_is_not_ok(self):
        self.assertFalse(ev.score("slow", diag("High latency", "rollback", "v1.0.0"))["action_ok"])

    def test_no_diagnosis_scores_zero(self):
        self.assertEqual(ev.score("leak", None),
                         {"reached": False, "cause_ok": False, "action_ok": False, "correct": False})

    def test_summary_and_markdown(self):
        results = [
            {"scenario": "leak", "run": 1, "seconds": 100, "score": ev.score("leak", diag("memory leak", "clear_fault"))},
            {"scenario": "leak", "run": 2, "seconds": 200, "score": ev.score("leak", None)},
        ]
        rows = ev.summarize(results)
        self.assertEqual((rows[0]["runs"], rows[0]["correct"], rows[0]["avg_s"]), (2, "50%", "150"))
        self.assertEqual(rows[-1]["scenario"], "Overall")
        md = ev.render_markdown(results, "m", "ollama")
        self.assertIn("| Memory leak | 2 |", md)

    def test_every_scenario_is_injectable(self):
        for spec in ev.SCENARIOS.values():
            path, body = spec["inject"]
            self.assertTrue(path.startswith("/admin/"))
            self.assertTrue(spec["cause_keywords"] and spec["actions"])


if __name__ == "__main__":
    unittest.main()
