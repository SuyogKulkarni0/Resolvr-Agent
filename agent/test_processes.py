"""Process simulation and the get_processes tool: no server needed."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("LOG_DIR", tempfile.mkdtemp())
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "demo_app"))
import app  # noqa: E402

import agent  # noqa: E402


class ProcessSimulationTest(unittest.TestCase):
    def setUp(self):
        app.reset()

    def tearDown(self):
        app.reset()

    def test_healthy_table_has_no_runaway_job(self):
        names = [p["name"] for p in app.processes()]
        self.assertIn("shop-api", names)
        self.assertNotIn("report-export", names)

    def test_runaway_process_tops_the_table_and_raises_memory(self):
        before = app.memory_mb()
        app.set_fault("rogue_process")
        with mock.patch.object(app.time, "time", return_value=app.rogue["started"] + 40):
            table = app.processes()
            self.assertEqual(table[0]["name"], "report-export")
            self.assertGreater(table[0]["cpu_pct"], 80)
            self.assertGreater(app.memory_mb(), 150)   # over the HighMemory limit
        self.assertLess(before, 60)

    def test_killing_it_restores_memory(self):
        app.set_fault("rogue_process")
        self.assertTrue(app.kill_process("report-export"))
        self.assertNotIn("report-export", [p["name"] for p in app.processes()])
        self.assertLess(app.memory_mb(), 60)

    def test_other_processes_cannot_be_killed(self):
        app.set_fault("rogue_process")
        self.assertFalse(app.kill_process("shop-api"))
        self.assertFalse(app.kill_process("report-export", pid=1))

    def test_restart_frees_leaked_memory(self):
        app.set_fault("leak")
        app.leak.extend(bytearray(1024 * 1024) for _ in range(20))
        self.assertGreater(app.memory_mb(), 50)
        self.assertTrue(app.restart_service("shop"))
        self.assertLess(app.memory_mb(), 50)
        self.assertFalse(app.restart_service("database"))


class ToolTest(unittest.TestCase):
    PROCS = [{"pid": 4121, "name": "report-export", "mem_mb": 180.0, "cpu_pct": 95.1},
             {"pid": 1, "name": "shop-api", "mem_mb": 22.0, "cpu_pct": 5.0}]

    def test_get_processes_is_a_registered_read_only_tool(self):
        self.assertIn("get_processes", [t["name"] for t in agent.TOOLS])
        with mock.patch.object(agent, "fetch", return_value=self.PROCS):
            self.assertEqual(json.loads(agent.run_tool("get_processes", {})), self.PROCS)

    def test_summary_names_the_hog(self):
        line = agent.summarize_result("get_processes", json.dumps(self.PROCS))
        self.assertIn("report-export (pid 4121) 180.0 MB", line)

    def test_action_label(self):
        self.assertEqual(agent.action_label({"type": "kill_process", "target": "report-export"}),
                         "kill_process report-export")
        self.assertEqual(agent.action_label({"type": "rollback", "target_version": "v1.0.0"}), "rollback to v1.0.0")


if __name__ == "__main__":
    unittest.main()
