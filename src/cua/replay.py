"""Deterministic replay: execute a capability with no model in the loop.

Per step, the engine runs the same small state machine::

    ┌─► detect conditions ──business──► return BusinessOutcome (terminal, not an error)
    │        │  recoverable ─► handle (dismiss / reload / re-auth) ─► loop
    │        │  fatal ───────► HardFailure
    │        ▼
    │   resolve target (first strategy with exactly ONE match; ambiguity ≠ success)
    │        │ none yet ─► wait (bounded) ─┘
    │        ▼
    │   policy gate (live control re-classified; irreversible needs authorization)
    │        ▼
    │   act ─► await post-condition (URL / element), still detecting conditions
    └────────┘ next step

Result contract (``ReplayResult.status``):

* ``success``           — outputs extracted, success checkpoint verified
* ``business_outcome``  — the app gave a legitimate answer (e.g. RECORD_NOT_FOUND)
* ``failure``           — hard failure: kind + step + expected + observed + evidence
* ``rejected``          — refused before touching the UI (bad inputs, unapproved/tampered artifact)

Recoveries that happened along the way, drift signals (a fallback strategy
had to be used) and human interventions are reported on *every* result,
so a success that needed help is distinguishable from a clean one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from .config import AppProfile, ConditionSpec, Dismiss, Reauthenticate, Reload, SecretStore
from .evidence import RunLog
from .handoff import ControlChannel, Intervention, _summarize
from .policy import Gate
from .redact import Redactor
from .schema import (
    Capability,
    Checkpoint,
    ElementPresent,
    Risk,
    Sensitivity,
    Step,
    Target,
    TextPresent,
    UrlIs,
    location_matches,
    parse_output,
    render,
)
from .surface.web import ActionError, WebSurface

FailureKind = Literal[
    "invalid_input",         # rejected: inputs violate the capability contract (UI never touched)
    "not_approved",          # rejected: draft/tampered artifact in unattended mode
    "target_not_found",      # nothing matched any strategy within the timeout
    "target_ambiguous",      # strategies matched >1 element; we refuse to guess
    "target_drifted",        # only a structural fallback matched, on a step where position is not proof
    "action_blocked",        # element found but could not be acted on (overlay, disabled)
    "checkpoint_failed",     # action ran but the expected state never appeared
    "app_error",             # host error page
    "permission_denied",     # operator entitlement problem (ops, not the member)
    "unexpected_dialog",     # native dialog not in the app profile
    "policy_violation",      # gate denied the action
    "approval_required",     # irreversible step without authorization
    "recovery_exhausted",    # a recoverable condition kept recurring
    "session_lost_after_commit",  # session died after an irreversible step: never auto-redo
    "output_parse_error",    # extracted text did not match the declared type
    "aborted_by_operator",
]


FALLBACK_GRACE_S = 1.0


class Failure(BaseModel):
    kind: FailureKind
    step: str | None
    intent: str | None
    expected: str
    observed: str
    retryable: bool
    evidence: list[str] = []


class Outcome(BaseModel):
    code: str
    condition: str
    description: str
    step: str | None


class ReplayResult(BaseModel):
    status: Literal["success", "business_outcome", "failure", "rejected"]
    capability: str
    tenant: str
    run_id: str
    started_at: str
    duration_ms: int = 0
    outputs: dict[str, Any] | None = None
    outcome: Outcome | None = None
    failure: Failure | None = None
    recoveries: list[dict[str, Any]] = []
    drift: list[dict[str, Any]] = []
    interventions: list[dict[str, Any]] = []
    steps_executed: int = 0


@dataclass
class ReplayOptions:
    allow_draft: bool = False
    # How to treat irreversible steps: 'authorized' = caller pre-authorized this
    # invocation; 'escalate' = ask a human at that step; 'block' = stop.
    irreversible: Literal["authorized", "escalate", "block"] = "block"
    on_failure: Literal["fail", "escalate"] = "fail"


class _Business(Exception):
    def __init__(self, outcome: Outcome) -> None:
        self.outcome = outcome


class _Hard(Exception):
    def __init__(self, kind: FailureKind, expected: str, observed: str, retryable: bool = False) -> None:
        super().__init__(f"{kind}: {observed}")
        self.kind, self.expected, self.observed, self.retryable = kind, expected, observed, retryable


class _Restart(Exception):
    """Session was re-established; restart the flow from the entry point."""


class Replayer:
    def __init__(self, cap: Capability, profile: AppProfile, tenant: str, surface: WebSurface, gate: Gate,
                 log: RunLog, redactor: Redactor, secrets: SecretStore, control: ControlChannel | None,
                 options: ReplayOptions, approval_error: str | None = None) -> None:
        self.cap = cap
        self.approval_error = approval_error
        self.profile = profile
        self.tenant = tenant
        self.s = surface
        self.gate = gate
        self.log = log
        self.redactor = redactor
        self.secrets = secrets
        self.control = control
        self.opt = options
        self.inputs: dict[str, str] = {}
        self.outputs: dict[str, Any] = {}
        self.attempts: dict[str, int] = {}
        self.recoveries: list[dict[str, Any]] = []
        self.drift: list[dict[str, Any]] = []
        self.interventions: list[dict[str, Any]] = []
        self.committed = False          # an irreversible step has executed
        self.executed = 0
        self.last_step: Step | None = None     # last step whose action executed
        self.current: Step | None = None       # step being attempted (for failure reports)
        self.mapping = {o.condition: o for o in cap.outcomes}

    # ================================================================== entry

    def run(self, raw_inputs: dict[str, Any]) -> ReplayResult:
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        res = ReplayResult(status="failure", capability=self.cap.ref, tenant=self.tenant, run_id=self.log.run_id,
                           started_at=started.isoformat(timespec="seconds"))
        try:
            self._preflight(raw_inputs)
        except _Hard as h:
            res.status = "rejected"
            res.failure = Failure(kind=h.kind, step=None, intent=None, expected=h.expected, observed=h.observed, retryable=False)
            return self._finish(res, t0)

        self.log.event("replay.start", capability=self.cap.ref, tenant=self.tenant,
                       inputs={k: v for k, v in self.inputs.items()}, options=self.opt.__dict__)
        restarts = 0
        while True:
            try:
                self.s.goto(self.cap.entry)
                self._run_steps()
                self._verify(self.cap.success, "success checkpoint", None)
                res.status, res.outputs = "success", self.outputs
                break
            except _Restart:
                restarts += 1
                if restarts > 2:
                    res.failure = Failure(kind="recovery_exhausted", step=None, intent=None, expected="stable session",
                                          observed="session lost repeatedly", retryable=True)
                    break
                self.log.event("replay.restart", reason="re-authenticated; restarting from entry", attempt=restarts)
                self.outputs.clear()
                continue
            except _Business as b:
                res.status, res.outcome = "business_outcome", b.outcome
                break
            except _Hard as h:
                res.failure = self._failure(h)
                break
        return self._finish(res, t0)

    def _finish(self, res: ReplayResult, t0: float) -> ReplayResult:
        res.duration_ms = int((time.monotonic() - t0) * 1000)
        res.recoveries, res.drift, res.interventions, res.steps_executed = (
            self.recoveries, self.drift, self.interventions, self.executed)
        self.log.event("replay.end", status=res.status, outcome=res.outcome and res.outcome.code,
                       failure=res.failure and res.failure.kind, duration_ms=res.duration_ms)
        # Persisted copy: outputs are redacted *structurally* by their declared
        # sensitivity (a parsed "12403.22" would not match the raw "$12,403.22"
        # string registered with the redactor). The caller gets the real values.
        persisted = res.model_dump(mode="json")
        if persisted["outputs"]:
            sens = {o.name: o.sensitivity for o in self.cap.outputs}
            persisted["outputs"] = {k: (v if sens.get(k) == Sensitivity.public else f"[{sens.get(k, 'redacted')}]")
                                    for k, v in persisted["outputs"].items()}
        self.log.write_json("result.json", persisted)
        return res

    # ================================================================== preflight

    def _preflight(self, raw: dict[str, Any]) -> None:
        # 1. contract: inputs validated *before* anything touches the UI
        errors = []
        declared = {p.name: p for p in self.cap.inputs}
        for extra in set(raw) - set(declared):
            errors.append(f"unknown input {extra!r}")
        for p in self.cap.inputs:
            if p.name not in raw:
                if p.required:
                    errors.append(f"missing required input {p.name!r}")
                continue
            try:
                self.inputs[p.name] = p.validate_value(raw[p.name])
                self.redactor.register(p.name, self.inputs[p.name], p.sensitivity)
            except ValueError as e:
                errors.append(str(e))
        if errors:
            raise _Hard("invalid_input", "inputs matching the capability contract", "; ".join(errors))
        # 2. approval: only approved, unmodified artifacts (and reviewed overlays)
        #    run unattended. Checked by the store against the *base* artifact.
        if self.approval_error and not self.opt.allow_draft:
            raise _Hard("not_approved", "approved, unmodified capability", self.approval_error)

    # ================================================================== steps

    def _run_steps(self) -> None:
        i = 0
        steps = self.cap.steps
        while i < len(steps):
            step = steps[i]
            try:
                self._step(step)
                i += 1
            except _Hard as h:
                # Policy violations are never handed to a human to "work around".
                if self.opt.on_failure != "escalate" or self.control is None or h.kind == "policy_violation":
                    raise
                iv = self._escalate("replay_failure", step, f"{h.kind}: {h.observed}", ["retry", "skip", "abort"],
                                    {"expected": h.expected, "observed": h.observed})
                if iv.resolution == "retry":
                    continue
                if iv.resolution == "skip":
                    # Human says they did this step. Trust but verify its post-condition.
                    if step.expect:
                        self._verify(step.expect, f"{step.id} post-condition after manual completion", step)
                    if step.action == "extract":
                        self._extract(step)
                    i += 1
                    continue
                raise _Hard("aborted_by_operator", h.expected, h.observed) from None

    def _step(self, step: Step) -> None:
        self.current = step
        self.log.event("step.begin", step=step.id, action=step.action, intent=step.intent, target=step.target.description)
        ref = self._await_target(step)
        if step.action == "extract":
            self._extract(step, ref)
            self.executed += 1
            return
        info = self.s.element_info(ref)
        decision = self.gate.check(step.action, info, recorded_risk=step.risk)
        self.log.event("policy", step=step.id, verdict=decision.verdict, risk=decision.risk.value, reason=decision.reason)
        if decision.verdict == "deny":
            raise _Hard("policy_violation", f"{step.action} allowed by policy", decision.reason)
        if decision.verdict == "needs_approval":
            self._authorize(step, decision.reason)
        if self.control:
            self.control.require_automation()
        try:
            self._act(step, ref)
        except ActionError as e:
            desc = self._render_target(step.target).description
            raise _Hard("action_blocked", f"{step.action} on {desc}", str(e), retryable=True) from None
        self.executed += 1
        self.last_step = step
        if decision.risk == Risk.irreversible:
            self.committed = True
        if step.expect:
            self._verify(step.expect, f"{step.id} post-condition", step)
        else:
            self.s.settle(200)
        self._check_conditions(step)
        self.log.event("step.ok", step=step.id, locations=self.s.locations())

    def _act(self, step: Step, ref: str) -> None:
        if step.action == "click":
            self.s.click(ref)
        elif step.action == "press":
            self.s.press(ref, step.key or "Enter")
        elif step.action in ("fill", "select"):
            v = step.value
            assert v is not None
            if v.secret:
                val = self.secrets.get(v.secret)
            elif v.input:
                if v.input not in self.inputs:
                    raise _Hard("policy_violation", f"input {v.input}", "optional input not supplied")
                val = self.inputs[v.input]
            else:
                val = v.literal or ""
            (self.s.fill if step.action == "fill" else self.s.select)(ref, val)

    def _extract(self, step: Step, ref: str | None = None) -> None:
        ref = ref or self._await_target(step)
        raw = self.s.read(ref) or ""
        field = next(o for o in self.cap.outputs if o.name == step.output)
        try:
            val = parse_output(field, raw)
        except Exception:
            raise _Hard("output_parse_error", f"{field.type.value} for {field.name}", repr(raw)) from None
        self.redactor.register(field.name, raw, field.sensitivity)
        self.outputs[field.name] = val
        self.log.event("extract", step=step.id, output=field.name, value=raw)

    def _authorize(self, step: Step, reason: str) -> None:
        if self.opt.irreversible == "authorized":
            self.log.event("irreversible.authorized", step=step.id, by="caller", reason=reason)
            return
        if self.opt.irreversible == "escalate" and self.control:
            iv = self._escalate("approval", step, f"irreversible step needs approval: {reason}", ["approve", "deny"], {})
            if iv.resolution == "approve":
                return
            raise _Hard("approval_required", "human approval", f"operator {iv.resolution}d the irreversible step")
        raise _Hard("approval_required", "authorization for irreversible step", reason, retryable=True)

    # ================================================================== waiting & detection

    def _render_target(self, t: Target) -> Target:
        return Target.model_validate(render(t.model_dump(), self.inputs))

    def _await_target(self, step: Step) -> str:
        target = self._render_target(step.target)
        deadline = time.monotonic() + step.timeout_ms / 1000
        t_start = time.monotonic()
        fallback_since: float | None = None
        while True:
            self._check_conditions(step)
            r = self.s.resolve(target)
            if r.ref and r.drifted:
                # Give the preferred strategy a short grace window before
                # accepting a fallback: the page may still be settling.
                fallback_since = fallback_since or time.monotonic()
                if time.monotonic() - fallback_since < FALLBACK_GRACE_S and time.monotonic() < deadline:
                    self.s.pump(150)
                    continue
            if r.ref:
                by = target.strategies[r.strategy_index or 0].by
                # Reading data for a caller, or committing a change, must be anchored
                # by meaning. A css path that still matches after the semantic
                # strategies failed means "something is at that position" — not
                # "the Balance of Share Savings". Refuse rather than return a wrong
                # number or press the wrong button.
                if by == "css" and r.drifted and (step.action == "extract" or step.risk == Risk.irreversible):
                    raise _Hard("target_drifted", f"{target.description} located by meaning",
                                f"semantic strategies matched {r.counts[:-1]}; only the structural path matched — "
                                "vendor/tenant variant? add an overlay", retryable=False)
                if r.drifted:
                    d = {"step": step.id, "target": target.description,
                         "resolved_by": target.strategies[r.strategy_index or 0].by, "counts": r.counts}
                    self.drift.append(d)
                    self.log.event("drift", **d)
                waited = int((time.monotonic() - t_start) * 1000)
                if waited > 2000:
                    self.log.event("slow", step=step.id, waited_ms=waited)
                return r.ref
            if time.monotonic() > deadline:
                observed = f"strategy match counts {r.counts} at {self.s.locations()}"
                if r.ambiguous:
                    raise _Hard("target_ambiguous", f"exactly one {target.description}", observed, retryable=True)
                raise _Hard("target_not_found", target.description, observed, retryable=True)
            self.s.pump(150)

    def _verify(self, cp: Checkpoint, what: str, step: Step | None, timeout_s: float | None = None) -> None:
        deadline = time.monotonic() + (timeout_s or (step.timeout_ms / 1000 if step else 10))
        while True:
            self._check_conditions(step)
            unmet = [c for c in cp.all_of if not self._holds(c)]
            if not unmet:
                return
            if time.monotonic() > deadline:
                exp = "; ".join(self._describe_cond(c) for c in unmet)
                raise _Hard("checkpoint_failed", f"{what}: {exp}", f"locations {self.s.locations()}", retryable=True)
            self.s.pump(150)

    def _holds(self, c) -> bool:
        if isinstance(c, UrlIs):
            actual = self.s.locations().get(c.frame)
            return actual is not None and location_matches(actual, render(c.path, self.inputs))
        if isinstance(c, ElementPresent):
            return self.s.resolve(self._render_target(c.target)).ref is not None
        if isinstance(c, TextPresent):
            return render(c.contains, self.inputs).lower() in self.s.frame_texts().get(c.frame, "").lower()
        return False

    def _describe_cond(self, c) -> str:
        if isinstance(c, UrlIs):
            return f"{c.frame} at {render(c.path, self.inputs)}"
        if isinstance(c, ElementPresent):
            return f"{c.target.description} present"
        return f"text {c.contains!r} in {c.frame}"

    def _check_conditions(self, step: Step | None) -> None:
        while self.s.known_dialogs:
            d = self.s.known_dialogs.pop(0)
            self.recoveries.append({"step": step.id if step else None, "condition": "native_dialog",
                                    "handler": d["action"], "attempt": 1})
        if self.s.unexpected_dialogs:
            msg = self.s.unexpected_dialogs.pop(0)
            raise _Hard("unexpected_dialog", "no unrecognised dialogs", f"dialog: {msg!r} (dismissed)", retryable=True)
        texts = self.s.frame_texts()
        for c in self.profile.conditions:
            frame = c.match.hit(texts)
            if frame is None:
                continue
            self._on_condition(c, frame, step)
            texts = self.s.frame_texts()  # state changed after handling

    def _on_condition(self, c: ConditionSpec, frame: str, step: Step | None) -> None:
        sid = step.id if step else None
        if c.klass == "business":
            m = self.mapping.get(c.id)
            raise _Business(Outcome(code=m.code if m else (c.code or c.id.upper()), condition=c.id,
                                    description=m.description if m else c.description, step=sid))
        if c.klass == "fatal":
            raise _Hard(c.kind or "app_error", "no host error", f"{c.id}: {c.description} (frame {frame})")  # type: ignore[arg-type]

        n = self.attempts[c.id] = self.attempts.get(c.id, 0) + 1
        rec = {"step": sid, "condition": c.id, "handler": c.handler.type if c.handler else None, "attempt": n}
        if n > c.max_attempts:
            raise _Hard("recovery_exhausted", f"{c.id} resolved within {c.max_attempts} attempts", f"{c.id} recurred", retryable=True)
        self.recoveries.append(rec)
        self.log.event("recovery", **rec)
        self.s.screenshot(self.log.next_shot(f"recovery-{c.id}"), self.redactor.literals)
        h = c.handler
        if isinstance(h, Dismiss):
            r = self.s.resolve(self._render_target(h.target))
            if not r.ref:
                raise _Hard("recovery_exhausted", f"dismiss control for {c.id}", "control not found")
            self.s.click(r.ref)
            self.s.settle()
        elif isinstance(h, Reload):
            if self.last_step is not None and not self.last_step.idempotent:
                raise _Hard("recovery_exhausted", "idempotent step before reload", f"{c.id} after non-idempotent {self.last_step.id}")
            self.s.pump(h.backoff_ms)
            self.s.reload_frame(frame)
            self.s.settle()
        elif isinstance(h, Reauthenticate):
            if self.committed:
                raise _Hard("session_lost_after_commit", "live session through commit",
                            "session expired after an irreversible step; outcome must be checked by a human")
            raise _Restart()

    # ================================================================== failure + escalation

    def _failure(self, h: _Hard) -> Failure:
        step = self.current
        shot = self.s.screenshot(self.log.next_shot(f"failure-{h.kind}"), self.redactor.literals)
        snap = self.log.write_text("failure-snapshot.txt", self.s.observe(opaque=True).text)
        f = Failure(kind=h.kind, step=step.id if step else None, intent=step.intent if step else None,
                    expected=h.expected, observed=self.redactor.text(h.observed), retryable=h.retryable,
                    evidence=[self.log.rel(shot), self.log.rel(snap)])
        self.log.event("failure", **f.model_dump())
        return f

    def _escalate(self, kind: str, step: Step, reason: str, allowed: list[str], ctx: dict[str, Any]) -> Intervention:
        assert self.control is not None
        iv = Intervention(run_id=self.log.run_id, kind=kind, subject=f"{self.cap.ref} on {self.tenant}",  # type: ignore[arg-type]
                          step=f"{step.id}: {step.intent}", reason=reason, allowed=allowed,  # type: ignore[arg-type]
                          context=ctx | {"target": step.target.description, "locations": self.s.locations(),
                                         "recoveries_so_far": self.recoveries})
        iv = self.control.escalate(iv)
        self.interventions.append({"id": iv.id, "kind": iv.kind, "step": step.id, "resolution": iv.resolution,
                                   "by": iv.claimed_by, "notes": iv.notes,
                                   "human_actions": [_summarize(a) for a in iv.human_actions]})
        return iv
