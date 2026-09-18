"""Discovery: an LLM-driven observe → decide → act loop against the live surface.

The model only ever sees (a) the goal, (b) input values it is allowed to
see, (c) *names* of secrets, (d) a redacted history, and (e) the current
text observation with refs. It answers with one JSON action. Everything it
does passes the policy gate; everything that succeeds becomes a trace entry
the recorder turns into an artifact. The model's prose ("thought") is kept
only as a one-line, redacted step intent — the artifact is not a transcript.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .evidence import RunLog
from .handoff import ControlChannel, Intervention, _summarize
from .llm import LLM
from .policy import Gate
from .schema import GoalSpec, Risk, parse_output
from .surface.web import ActionError, WebSurface

SYSTEM_PROMPT = """You operate a legacy back-office web application for a credit union, one action at a time.

You receive: the GOAL, INPUT values, the names of SECRETS you may type (you never see their values),
the OUTPUTS you must extract, the HISTORY of your previous actions, and an OBSERVATION of every frame.
In the observation each control or table cell has a ref like [main:e12]. Rows of tables are shown as
`row: [ref] "cell" | [ref] "cell"`; form controls show `label=` when they only have a caption beside them.

Respond with ONE JSON object and nothing else:
{"thought": "<one short sentence: why this action>",
 "action": "click" | "fill" | "select" | "press" | "extract" | "wait" | "done" | "give_up",
 "ref": "<ref from the CURRENT observation>",          // click, fill, select, press, extract
 "input": "<input name>" | "secret": "<secret name>" | "text": "<literal>",   // fill / select (one of them)
 "key": "Enter",                                         // press
 "output": "<output name>",                              // extract: ref must be the element whose text is the value
 "success_ref": "<ref proving the goal is reached>",     // done
 "reason": "<why you cannot continue>"}                  // give_up

Rules:
- Only use refs present in the CURRENT observation. Refs change after every action.
- When typing a provided input value use {"input": name}; for credentials use {"secret": name}. Never type credentials as text.
- Extract each OUTPUT exactly once, from the cell/element that contains only that value.
- Prefer the most direct path. Do not open menus or pages unrelated to the goal.
- If a page shows an error, notice, or unexpected state, deal with it or give_up with a clear reason.
- A control that commits a change (confirm/submit/open) may require human approval; propose it normally when the goal needs it.
- Call "done" only after all outputs are extracted and the goal state is visible; success_ref must show that state.
"""


@dataclass
class TraceEntry:
    n: int
    action: str
    info: dict[str, Any]              # element_info at action time (role, name, strategies, frame...)
    value: dict[str, str] | None      # {"input": ..} | {"secret": ..} | {"literal": ..}
    key: str | None
    output: str | None
    thought: str
    risk: Risk
    before: dict[str, str]
    after: dict[str, str]
    provenance: str = "model"


@dataclass
class DiscoveryResult:
    status: str                      # completed | failed | aborted
    reason: str
    trace: list[TraceEntry] = field(default_factory=list)
    extracted: dict[str, str] = field(default_factory=dict)
    success_info: dict[str, Any] | None = None
    final_locations: dict[str, str] = field(default_factory=dict)
    steps_used: int = 0
    model: str = ""
    human_steps: int = 0


def _parse_json(text: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


def _describe(info: dict[str, Any]) -> str:
    """Log-safe description. A cell's accessible name *is* its data (a name, a
    balance), so cells are described by table position, never by content."""
    if info.get("role") == "cell":
        tc = next((s for s in info.get("strategies", []) if s.get("by") == "table_cell"), None)
        where = f'column {tc["column"]!r} of row {tc["row"]["column"]}={tc["row"]["equals"]!r}' if tc else "table cell"
        return f"cell at {where} in {info.get('frame')}"
    return f'{info.get("role")} "{info.get("name") or info.get("label") or "?"}" in {info.get("frame")}'


class DiscoveryAgent:
    def __init__(self, spec: GoalSpec, surface: WebSurface, gate: Gate, llm: LLM, log: RunLog,
                 control: ControlChannel | None, secret_names: list[str], secret_value) -> None:
        self.spec = spec
        self.surface = surface
        self.gate = gate
        self.llm = llm
        self.log = log
        self.control = control
        self.secret_names = secret_names
        self.secret_value = secret_value
        self.inputs = {i.name: i.example for i in spec.inputs}
        self.history: list[str] = []

    # ------------------------------------------------------------------ prompt

    def _prompt(self, obs_text: str, n: int, extracted: dict[str, str]) -> str:
        outs = "\n".join(
            f"- {o.name} ({o.type.value}): {o.description}" + ("  [DONE]" if o.name in extracted else "")
            for o in self.spec.outputs
        )
        ins = "\n".join(f"- {i.name} = {i.example!r}  ({i.description})" for i in self.spec.inputs)
        hist = "\n".join(self.history[-14:]) or "(none yet)"
        return (
            f"GOAL: {self.spec.rendered_goal()}\n\nINPUTS:\n{ins}\n\nSECRETS: {', '.join(self.secret_names)}\n\n"
            f"OUTPUTS TO EXTRACT:\n{outs}\n\nHISTORY:\n{hist}\n\nSTEP {n} of {self.spec.max_steps}\n\n"
            f"OBSERVATION:\n{obs_text}\n"
        )

    # ------------------------------------------------------------------ escalation

    def _escalate(self, kind: str, n: int, reason: str, allowed: list[str], ctx: dict[str, Any]) -> Intervention | None:
        if self.control is None:
            return None
        iv = Intervention(
            run_id=self.log.run_id, kind=kind, subject=self.spec.rendered_goal(),
            step=f"discovery step {n}", reason=reason, allowed=allowed,  # type: ignore[arg-type]
            context=ctx | {"locations": self.surface.locations()},
        )
        return self.control.escalate(iv)

    def _absorb_human(self, iv: Intervention, trace: list[TraceEntry], n: int) -> int:
        """Record what the human did, as trace entries (clicks/selects are replayable; typed values are not captured)."""
        added = 0
        for ev in iv.human_actions:
            tgt = ev.get("target") or {}
            info = tgt | {"frame": ev.get("frame")}
            if ev["type"] == "click" and tgt.get("role") in ("link", "button"):
                trace.append(TraceEntry(n, "click", info, None, None, None, "operator action", self.gate.classify("click", info),
                                        {}, self.surface.locations(), provenance="human"))
                added += 1
            elif ev["type"] == "select" and ev.get("option"):
                trace.append(TraceEntry(n, "select", info, {"literal": ev["option"]}, None, None, "operator action",
                                        Risk.reversible, {}, self.surface.locations(), provenance="human"))
                added += 1
        summary = "; ".join(f'{s["type"]} {s["role"]} "{s["name"]}"' for s in map(_summarize, iv.human_actions)) or "no UI actions"
        self.history.append(f"-- HUMAN OPERATOR intervened ({iv.resolution}): {summary}. Notes: {iv.notes or '-'}")
        return added

    # ------------------------------------------------------------------ loop

    def run(self, deadline_s: int = 600) -> DiscoveryResult:
        res = DiscoveryResult(status="failed", reason="", model=self.llm.name)
        t_end = time.monotonic() + deadline_s
        repeats: dict[tuple, int] = {}
        errors_in_row = 0
        n = 0
        while True:
            n += 1
            res.steps_used = n
            if n > self.spec.max_steps or time.monotonic() > t_end:
                why = "max steps reached" if n > self.spec.max_steps else "time budget exhausted"
                iv = self._escalate("stuck", n, why, ["continue", "abort"], {})
                if iv and iv.resolution == "continue":
                    self.spec.max_steps += 10
                    t_end = time.monotonic() + deadline_s
                    continue
                res.reason = why
                return res
            self.surface.settle(150)
            obs = self.surface.observe()
            self.log.event("observe", step=n, digest=obs.digest, locations=obs.locations, chars=len(obs.text))

            try:
                reply = self.llm.complete(SYSTEM_PROMPT, self._prompt(obs.text, n, res.extracted))
                d = _parse_json(reply.text)
                res.model = reply.model
            except Exception as e:  # malformed reply or backend failure
                errors_in_row += 1
                self.log.event("llm.error", step=n, error=str(e)[:300])
                self.history.append(f"{n}. (invalid response: {str(e)[:80]}) — reply with ONE JSON object")
                if errors_in_row >= 3:
                    iv = self._escalate("stuck", n, f"model failed to produce a valid action 3x: {e}", ["continue", "abort"], {})
                    if not iv or iv.resolution != "continue":
                        res.reason = "model errors"
                        return res
                    errors_in_row = 0
                continue

            act = d.get("action")
            thought = str(d.get("thought", ""))[:200]
            self.log.event("decide", step=n, action=act, ref=d.get("ref"), thought=thought,
                           value_source={k: d[k] for k in ("input", "secret") if k in d} or ("literal" if "text" in d else None),
                           output=d.get("output"), tokens={"in": reply.input_tokens, "out": reply.output_tokens})

            # --- stuck detection: same decision on an unchanged screen
            sig = (obs.digest, act, d.get("ref"), d.get("output"))
            repeats[sig] = repeats.get(sig, 0) + 1
            if repeats[sig] >= 3 or errors_in_row >= 3:
                iv = self._escalate("stuck", n, f"repeating {act} {d.get('ref')} with no progress", ["continue", "abort"],
                                    {"last_thought": thought})
                if not iv or iv.resolution != "continue":
                    res.reason = "stuck: repeated action without progress"
                    return res
                res.human_steps += self._absorb_human(iv, res.trace, n)
                repeats.clear()
                errors_in_row = 0
                continue

            if act == "give_up":
                iv = self._escalate("stuck", n, f"agent gave up: {d.get('reason')}", ["continue", "abort"], {"last_thought": thought})
                if not iv or iv.resolution != "continue":
                    res.status, res.reason = ("aborted" if iv else "failed"), f"gave up: {d.get('reason')}"
                    return res
                res.human_steps += self._absorb_human(iv, res.trace, n)
                continue

            if act == "wait":
                self.surface.pump(1500)
                self.history.append(f"{n}. wait")
                continue

            if act == "done":
                missing = [o.name for o in self.spec.outputs if o.name not in res.extracted]
                if missing:
                    self.history.append(f"{n}. done REJECTED: outputs not extracted yet: {missing}")
                    errors_in_row += 1
                    continue
                try:
                    res.success_info = self.surface.element_info(d["success_ref"])
                except Exception as e:
                    self.history.append(f"{n}. done REJECTED: success_ref invalid ({e})")
                    errors_in_row += 1
                    continue
                res.status, res.reason = "completed", str(d.get("thought", ""))[:200]
                res.final_locations = self.surface.locations()
                self.log.event("done", step=n, success=_describe(res.success_info), locations=res.final_locations)
                return res

            # --- concrete action on an element
            try:
                entry = self._act(n, d, thought, res)
                errors_in_row = 0
                if entry:
                    res.trace.append(entry)
            except _Feedback as fb:
                errors_in_row += 1
                self.history.append(f"{n}. {act} {d.get('ref')} FAILED: {fb}")
                self.log.event("act.rejected", step=n, action=act, ref=d.get("ref"), why=str(fb))
            except _Abort as ab:
                res.status, res.reason = "aborted", str(ab)
                return res

    def _act(self, n: int, d: dict[str, Any], thought: str, res: DiscoveryResult) -> TraceEntry | None:
        act = d.get("action")
        ref = d.get("ref")
        if act not in ("click", "fill", "select", "press", "extract") or not ref:
            raise _Feedback(f"unknown action {act!r} or missing ref")
        try:
            info = self.surface.element_info(ref)
        except ActionError as e:
            raise _Feedback(f"{e}; use a ref from the current observation") from None

        decision = self.gate.check(act, info)
        self.log.event("policy", step=n, action=act, target=_describe(info), verdict=decision.verdict,
                       risk=decision.risk.value, reason=decision.reason)
        if decision.verdict == "deny":
            raise _Feedback(f"DENIED by policy: {decision.reason}")
        if decision.verdict == "needs_approval":
            iv = self._escalate("approval", n, f"irreversible action proposed: {act} {_describe(info)}",
                                ["approve", "deny", "abort"], {"proposed": {"action": act, "target": _describe(info)},
                                                               "thought": thought})
            if iv is None:
                raise _Feedback("irreversible action blocked: no operator available to approve")
            if iv.resolution == "abort":
                raise _Abort("operator aborted at approval gate")
            if iv.resolution != "approve":
                raise _Feedback("operator DENIED this irreversible action; do not retry it")
            self.history.append(f"-- operator APPROVED: {act} {_describe(info)}")

        value: dict[str, str] | None = None
        text_val: str | None = None
        if act in ("fill", "select"):
            if "secret" in d:
                if d["secret"] not in self.secret_names:
                    raise _Feedback(f"unknown secret {d['secret']!r}")
                value, text_val = {"secret": d["secret"]}, self.secret_value(d["secret"])
            elif "input" in d:
                if d["input"] not in self.inputs:
                    raise _Feedback(f"unknown input {d['input']!r}")
                value, text_val = {"input": d["input"]}, self.inputs[d["input"]]
            elif "text" in d:
                text_val = str(d["text"])
                # A literal that equals an input value is that input (keeps the artifact parameterized).
                match = next((k for k, v in self.inputs.items() if v == text_val), None)
                value = {"input": match} if match else {"literal": text_val}
            else:
                raise _Feedback("fill/select needs input, secret or text")

        before = self.surface.locations()
        if self.control:
            self.control.require_automation()
        try:
            if act == "click":
                self.surface.click(ref)
            elif act == "fill":
                self.surface.fill(ref, text_val or "")
            elif act == "select":
                self.surface.select(ref, text_val or "")
            elif act == "press":
                self.surface.press(ref, d.get("key") or "Enter")
            elif act == "extract":
                out = d.get("output")
                spec_out = next((o for o in self.spec.outputs if o.name == out), None)
                if spec_out is None:
                    raise _Feedback(f"unknown output {out!r}")
                raw = self.surface.read(ref) or ""
                try:
                    parse_output(spec_out, raw)
                except Exception:
                    raise _Feedback(f"text {raw!r} does not parse as {spec_out.type.value}") from None
                res.extracted[out] = raw
                self.log.redactor.register(out, raw, spec_out.sensitivity)
        except ActionError as e:
            raise _Feedback(str(e)) from None

        self.surface.settle()
        after = self.surface.locations()
        changed = {k: v for k, v in after.items() if before.get(k) != v}
        src = f" ← {next(iter(value))}:{next(iter(value.values()))}" if value and "literal" not in value else ""
        extra = f" = {res.extracted[d['output']]!r}" if act == "extract" else ""
        self.history.append(f"{n}. {act} {_describe(info)}{src}{extra} → ok" + (f"; now {changed}" if changed else ""))
        self.log.event("act", step=n, action=act, target=_describe(info), value_source=value and next(iter(value)),
                       changed=changed, blocked=self.surface.blocked_requests[-1:] if self.surface.blocked_requests else [])
        return TraceEntry(n, act, info, value, d.get("key"), d.get("output"), thought, decision.risk, before, after)


class _Feedback(Exception):
    """Recoverable: tell the model what went wrong and let it choose again."""


class _Abort(Exception):
    pass
