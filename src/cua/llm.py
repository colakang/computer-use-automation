"""LLM backends. The agent loop needs exactly one thing: system+user text in, text out.

* ``claude-cli``  — shells out to ``claude -p`` (Claude Code headless) with all
  tools disabled; uses the developer's existing Claude login. Used for the
  recorded discovery runs in /evidence.
* ``anthropic``   — the Anthropic Messages API (needs ANTHROPIC_API_KEY).
* ``scripted``    — replays canned decisions; used by tests.

Stateless per call on purpose: each decision is made from (goal, redacted
history, current observation). No hidden conversation state means a run can
pause for a human for ten minutes and resume without anything going stale.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Protocol


@dataclass
class LLMReply:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLM(Protocol):
    name: str

    def complete(self, system: str, user: str) -> LLMReply: ...


class ClaudeCLI:
    def __init__(self, model: str = "sonnet") -> None:
        if not shutil.which("claude"):
            raise RuntimeError("claude CLI not found; install Claude Code or use --llm anthropic")
        self.model = model
        self.name = f"claude-cli:{model}"

    def complete(self, system: str, user: str) -> LLMReply:
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--tools", "",
            "--output-format", "json",
            "--no-session-persistence",
            "--setting-sources", "",
            "--strict-mcp-config",
            "--system-prompt", system,
        ]
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr[-500:] or proc.stdout[-500:]}")
        out = json.loads(proc.stdout)
        if out.get("is_error"):
            raise RuntimeError(f"claude -p error: {out.get('result')}")
        usage = out.get("usage", {})
        model = next(iter(out.get("modelUsage", {}) or {}), self.model)
        tokens_in = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        return LLMReply(out["result"], model, tokens_in, usage.get("output_tokens", 0))


class AnthropicAPI:
    def __init__(self, model: str = "claude-sonnet-5") -> None:
        try:
            import anthropic
        except ImportError as e:  # optional dependency
            raise RuntimeError("pip install 'cua[anthropic]' to use the API backend") from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"

    def complete(self, system: str, user: str) -> LLMReply:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return LLMReply(text, msg.model, msg.usage.input_tokens, msg.usage.output_tokens)


class Scripted:
    """Deterministic stand-in for tests. Each queued item is a reply dict, or a
    callable that receives the prompt (so a test can pick refs from the live
    observation the way the model would)."""

    def __init__(self, replies: list) -> None:
        self._replies = list(replies)
        self.name = "scripted"
        self.prompts: list[str] = []

    def complete(self, system: str, user: str) -> LLMReply:
        self.prompts.append(user)
        if not self._replies:
            return LLMReply(json.dumps({"thought": "out of script", "action": "give_up", "reason": "script exhausted"}), "scripted")
        nxt = self._replies.pop(0)
        return LLMReply(json.dumps(nxt(user) if callable(nxt) else nxt), "scripted")


def make_llm(kind: str | None = None, model: str | None = None) -> LLM:
    kind = kind or os.environ.get("CUA_LLM", "claude-cli")
    if kind == "claude-cli":
        return ClaudeCLI(model or "sonnet")
    if kind == "anthropic":
        return AnthropicAPI(model or "claude-sonnet-5")
    raise ValueError(f"unknown LLM backend {kind!r}")
