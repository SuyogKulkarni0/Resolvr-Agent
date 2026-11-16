"""
Ollama backend: lets the agent run on a free local model, with no API key.

It exposes the same `client.messages.create(...)` shape the agent already uses with
the Anthropic SDK, and translates to and from Ollama's /api/chat format. The agent
loop, tools and guardrails are therefore identical for both backends.
"""
import json
import os
import urllib.request
from types import SimpleNamespace as NS

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5:7b"


def to_ollama_tools(tools):
    return [{"type": "function",
             "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]


def _get(block, key):
    return block[key] if isinstance(block, dict) else getattr(block, key)


def to_ollama_messages(system, messages):
    out = [{"role": "system", "content": system}]
    tool_names = {}  # tool_use id -> tool name, so results can be labelled
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
        elif m["role"] == "assistant":
            text = " ".join(_get(b, "text") for b in content if _get(b, "type") == "text")
            calls = []
            for b in content:
                if _get(b, "type") == "tool_use":
                    tool_names[_get(b, "id")] = _get(b, "name")
                    calls.append({"function": {"name": _get(b, "name"), "arguments": _get(b, "input")}})
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
        else:  # user turn carrying tool results
            for b in content:
                if b["type"] == "tool_result":
                    out.append({"role": "tool", "tool_name": tool_names.get(b["tool_use_id"], ""),
                                "content": b["content"]})
    return out


def from_ollama_message(message):
    blocks = []
    if message.get("content", "").strip():
        blocks.append(NS(type="text", text=message["content"]))
    for i, call in enumerate(message.get("tool_calls") or []):
        args = call["function"].get("arguments") or {}
        if isinstance(args, str):  # some models return arguments as a JSON string
            args = json.loads(args)
        blocks.append(NS(type="tool_use", id=f"call_{i}_{call['function']['name']}",
                         name=call["function"]["name"], input=args))
    return blocks


class OllamaClient:
    def __init__(self, url=OLLAMA_URL, timeout=300):
        self.url, self.timeout = url, timeout
        self.messages = NS(create=self.create)

    def create(self, model, system, tools, messages, max_tokens=1500, **_):
        payload = {"model": model, "stream": False,
                   "messages": to_ollama_messages(system, messages),
                   "tools": to_ollama_tools(tools),
                   "options": {"temperature": 0, "num_predict": max_tokens}}
        req = urllib.request.Request(self.url + "/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            reply = json.loads(r.read())
        return NS(content=from_ollama_message(reply["message"]))
