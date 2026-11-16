"""Dashboard chat box: intent routing and live answers. Runs the app's functions in-process, no server."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo_app"))
import app  # noqa: E402
import chat  # noqa: E402


def ask(q):
    return app.chat_answer(q)


def text(reply):
    return " | ".join(r["t"] for r in reply["rows"])


class IntentTest(unittest.TestCase):
    def test_example_questions_route_to_the_right_topic(self):
        cases = {"how is the database?": "database", "memory usage": "memory", "what's the cpu load": "cpu",
                 "top processes": "processes", "any alerts?": "alerts", "recent errors in the logs": "logs",
                 "latency and errors": "metrics", "last deploy": "deploys", "how are the services?": "services",
                 "is inventory ok": "inventory", "help": "help", "purple monkey dishwasher": "unknown"}
        for question, intent in cases.items():
            self.assertEqual(chat.intent_of(question), intent, question)


class LiveAnswerTest(unittest.TestCase):
    def setUp(self):
        app.reset()

    def tearDown(self):
        app.reset()

    def test_healthy_services(self):
        reply = ask("how are the services?")
        self.assertEqual(reply["title"], "Service status")
        self.assertTrue(all(r["s"] == "ok" for r in reply["rows"]), text(reply))

    def test_database_degraded_under_db_errors(self):
        app.set_fault("errors")
        reply = ask("how is the database?")
        self.assertEqual(reply["rows"][0]["s"], "warn")
        self.assertIn("connection pool exhausted", text(reply))

    def test_inventory_down(self):
        app.set_fault("inventory_down")
        reply = ask("is inventory up?")
        self.assertEqual(reply["rows"][0]["s"], "bad")
        self.assertIn("connection refused", text(reply))

    def test_memory_names_the_runaway_process(self):
        app.set_fault("rogue_process")
        with mock.patch.object(app.time, "time", return_value=app.rogue["started"] + 40):
            reply = ask("memory usage")
        self.assertEqual(reply["rows"][0]["s"], "bad")
        self.assertIn("report-export", text(reply))

    def test_cpu_flags_the_busy_process(self):
        app.set_fault("rogue_process")
        reply = ask("cpu usage")
        self.assertTrue(any(r["s"] == "bad" and "report-export" in r["t"] for r in reply["rows"]), text(reply))

    def test_asking_never_changes_anything(self):
        app.set_fault("rogue_process")
        before = (dict(app.state), app.rogue["started"], len(app.log_buffer))
        for q in ["memory", "cpu", "database", "services", "kill report-export", "restart the shop", "rollback"]:
            ask(q)
        self.assertEqual((dict(app.state), app.rogue["started"], len(app.log_buffer)), before)

    def test_unknown_question_gets_help_not_a_guess(self):
        reply = ask("purple monkey dishwasher")
        self.assertEqual(reply["intent"], "unknown")
        self.assertEqual(reply["rows"][0]["s"], "warn")


if __name__ == "__main__":
    unittest.main()
