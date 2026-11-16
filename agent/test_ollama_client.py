"""Ollama adapter tests: translation only, no Ollama needed."""
import io
import json
import unittest
from types import SimpleNamespace as NS
from unittest import mock

import agent
import ollama_client as oc


class AdapterTest(unittest.TestCase):
    def test_tools_are_converted_to_function_format(self):
        out = oc.to_ollama_tools(agent.TOOLS)
        self.assertEqual(len(out), len(agent.TOOLS))
        self.assertEqual(out[0]["type"], "function")
        self.assertIn("parameters", out[0]["function"])

    def test_conversation_with_tool_round_trip(self):
        messages = [
            {"role": "user", "content": "investigate"},
            {"role": "assistant", "content": [NS(type="text", text="checking"),
                                              NS(type="tool_use", id="t1", name="get_alerts", input={})]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "[]"}]},
        ]
        out = oc.to_ollama_messages("sys", messages)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "tool"])
        self.assertEqual(out[2]["tool_calls"][0]["function"]["name"], "get_alerts")
        self.assertEqual(out[3]["tool_name"], "get_alerts")

    def test_response_tool_calls_become_tool_use_blocks(self):
        blocks = oc.from_ollama_message({"content": "", "tool_calls": [
            {"function": {"name": "get_logs", "arguments": {"n": 5}}},
            {"function": {"name": "get_deploys", "arguments": "{}"}}]})  # string args handled
        self.assertEqual([b.type for b in blocks], ["tool_use", "tool_use"])
        self.assertEqual(blocks[0].input, {"n": 5})
        self.assertEqual(blocks[1].input, {})

    def test_agent_loop_runs_on_ollama_client(self):
        diagnosis = {"root_cause": "x", "evidence": ["y"],
                     "proposed_action": {"type": "none", "detail": "d"}, "confidence": 0.5}
        replies = [
            {"message": {"content": "", "tool_calls": [{"function": {"name": "get_alerts", "arguments": {}}}]}},
            {"message": {"content": "", "tool_calls": [{"function": {"name": "submit_diagnosis", "arguments": diagnosis}}]}},
        ]

        def fake_urlopen(req, timeout=None):
            return io.BytesIO(json.dumps(replies.pop(0)).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch.object(agent, "fetch", return_value=[]):
            result = agent.run_triage(oc.OllamaClient(), verbose=False, model="qwen2.5:7b")
        self.assertEqual(result, diagnosis)


if __name__ == "__main__":
    unittest.main()
