"""Tests the agent loop with a scripted fake model (no API key or running service needed)."""
import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import agent


def tool_use(id_, name, **inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeClient:
    """Returns scripted responses in order, like client.messages.create."""
    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = NS(create=self.create)

    def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot: the agent mutates the list later
        return NS(content=self.script.pop(0))


class AgentLoopTest(unittest.TestCase):
    def test_investigates_then_submits_diagnosis(self):
        diagnosis = {"root_cause": "bad deploy v1.1.0", "evidence": ["KeyError in logs"],
                     "proposed_action": {"type": "rollback", "detail": "v1.0.0"}, "confidence": 0.9}
        client = FakeClient([
            [tool_use("1", "get_alerts"), tool_use("2", "get_deploys")],
            [tool_use("3", "submit_diagnosis", **diagnosis)],
        ])
        with mock.patch.object(agent, "fetch", return_value=[{"alert": "HighErrorRate"}]) as f:
            result = agent.run_triage(client, verbose=False)
        self.assertEqual(result, diagnosis)
        self.assertEqual(f.call_count, 2)
        # tool results were sent back to the model on the second turn
        sent = client.calls[1]["messages"][-1]["content"]
        self.assertEqual([r["tool_use_id"] for r in sent], ["1", "2"])

    def test_gives_up_after_max_turns(self):
        client = FakeClient([[tool_use(str(i), "get_metrics")] for i in range(agent.MAX_TURNS)])
        with mock.patch.object(agent, "fetch", return_value={}):
            self.assertIsNone(agent.run_triage(client, verbose=False))

    def test_tool_errors_are_returned_not_raised(self):
        with mock.patch.object(agent, "fetch", side_effect=OSError("connection refused")):
            out = json.loads(agent.run_tool("get_alerts", {}))
        self.assertIn("connection refused", out["error"])

    def test_agent_has_no_write_tools(self):
        names = {t["name"] for t in agent.TOOLS}
        self.assertEqual(names, {"get_alerts", "get_metrics", "get_logs", "get_deploys", "submit_diagnosis"})


if __name__ == "__main__":
    unittest.main()
