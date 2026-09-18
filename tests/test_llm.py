"""LLM backends, without network: both adapters parse their transport correctly."""

import json
import sys
import types

import pytest

from cua import llm


def test_claude_cli_adapter(monkeypatch):
    seen = {}

    def fake_run(cmd, input, capture_output, text, timeout):
        seen["cmd"], seen["input"] = cmd, input
        out = {"result": '{"action": "click", "ref": "main:e1"}', "is_error": False,
               "usage": {"input_tokens": 3, "cache_read_input_tokens": 1500, "output_tokens": 20},
               "modelUsage": {"claude-sonnet-5": {}}}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    r = llm.ClaudeCLI("sonnet").complete("SYS", "USER")
    assert json.loads(r.text)["ref"] == "main:e1" and r.model == "claude-sonnet-5" and r.input_tokens == 1503
    assert seen["cmd"][seen["cmd"].index("--tools") + 1] == "", "model gets no tools of its own"
    assert seen["input"] == "USER"


def test_claude_cli_error_surfaces(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    with pytest.raises(RuntimeError, match="boom"):
        llm.ClaudeCLI().complete("s", "u")


def test_anthropic_adapter(monkeypatch):
    calls = {}

    class Messages:
        def create(self, **kw):
            calls.update(kw)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text='{"action": "done"}')],
                model="claude-sonnet-5", usage=types.SimpleNamespace(input_tokens=10, output_tokens=5))

    fake = types.ModuleType("anthropic")
    fake.Anthropic = lambda: types.SimpleNamespace(messages=Messages())
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    r = llm.make_llm("anthropic").complete("SYS", "USER")
    assert r.text == '{"action": "done"}' and calls["system"] == "SYS" and calls["model"] == "claude-sonnet-5"
