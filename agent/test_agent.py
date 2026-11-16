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

    def test_repeated_logs_are_collapsed(self):
        e = {"ts": "2026-11-16T09:01:27+00:00", "level": "ERROR", "version": "v1.1.0", "msg": "KeyError"}
        out = agent.summarize_logs([e, {**e, "ts": "2026-11-16T09:01:30+00:00"}, {**e, "msg": "other"}])
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["count"], out[0]["first"], out[0]["last"]), (2, "09:01:27", "09:01:30"))

    def test_json_diagnosis_in_text_is_accepted(self):
        d = {"root_cause": "bad deploy", "evidence": ["a"], "confidence": 0.9,
             "proposed_action": {"type": "rollback", "detail": "x", "target_version": "v1.0.0"}}
        text = "Based on evidence...\n```json\n" + json.dumps(d) + "\n```"
        self.assertEqual(agent.extract_diagnosis(text), d)
        client = FakeClient([[NS(type="text", text=text)]])
        self.assertEqual(agent.run_triage(client, verbose=False), d)

    def test_malformed_diagnosis_is_rejected(self):
        self.assertIsNone(agent.extract_diagnosis("no json here"))
        bad = {"root_cause": "x", "evidence": [], "confidence": 7,
               "proposed_action": {"type": "format_disk", "detail": ""}}
        self.assertIsNone(agent.extract_diagnosis(json.dumps(bad)))

    def test_invalid_submit_gets_error_and_can_retry(self):
        good = {"root_cause": "x", "evidence": ["y"], "confidence": 0.8,
                "proposed_action": {"type": "none", "detail": "d"}}
        client = FakeClient([[tool_use("1", "submit_diagnosis", root_cause="x")],
                             [tool_use("2", "submit_diagnosis", **good)]])
        self.assertEqual(agent.run_triage(client, verbose=False), good)

    def test_agent_has_no_write_tools(self):
        names = {t["name"] for t in agent.TOOLS}
        self.assertEqual(names, {"get_alerts", "get_metrics", "get_logs", "get_deploys", "get_processes",
                                 "submit_diagnosis"})


if __name__ == "__main__":
    unittest.main()
