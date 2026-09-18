"""Turn a successful discovery trace into a Capability artifact.

What the recorder decides (and why):

* **Keep only strategies that were unique at record time**, in robustness
  order (role+name → visual label → table cell → attribute → css path).
  A strategy that already matched two things when we saw the page is
  worse than useless on replay.
* **Parameterize** every literal that equals an input example — in values,
  locator fields and URL checkpoints — so `/member/detail?id=10042`
  becomes `/member/detail?id={{member_id}}` and `link "10042"` becomes
  `link "{{member_id}}"`.
* **Checkpoint every navigation**: if an action changed a frame's URL, the
  new URL becomes that step's post-condition. Replay then knows *that the
  click worked*, not just that it didn't throw.
* **Phase**: steps up to the submit after the last credential are
  ``auth``; the rest are ``main``. Reviewers see the sign-on prefix at a
  glance; re-auth currently restarts from the entry (see replay.py).
* **Business outcomes** come from the app profile, not the model: the
  model saw only the happy path.
* **Lint**: refuse to write an artifact that contains any registered
  sensitive literal (a secret, a PII/confidential value seen during the run).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .agent import DiscoveryResult, TraceEntry
from .config import AppProfile, TenantConfig
from .redact import Redactor
from .schema import (
    AppRef,
    Capability,
    Checkpoint,
    ElementPresent,
    GoalSpec,
    InputParam,
    OutcomeMapping,
    Provenance,
    Risk,
    Step,
    Target,
    UrlIs,
    ValueRef,
    split_location,
)

STRATEGY_ORDER = ["role_name", "label", "table_cell", "attr", "css"]
_STRATEGY_FIELDS = {"role_name", "label", "table_cell", "attr", "css"}


class RecorderError(Exception):
    pass


def _templatize(obj: Any, examples: dict[str, str]) -> Any:
    if isinstance(obj, str):
        for name, val in sorted(examples.items(), key=lambda kv: -len(kv[1])):
            if val and len(val) >= 3:
                obj = re.sub(rf"(?<![\w]){re.escape(val)}(?![\w])", "{{" + name + "}}", obj)
        return obj
    if isinstance(obj, dict):
        return {k: _templatize(v, examples) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_templatize(v, examples) for v in obj]
    return obj


def templatize_location(loc: str, examples: dict[str, str]) -> str:
    """Parameterize a recorded URL: query values are compared *decoded*, and
    written back decoded (the replay comparison decodes too)."""
    path, pairs = split_location(loc)
    by_value = {v: k for k, v in examples.items() if v}
    path = _templatize(path, examples)
    if not pairs:
        return path
    q = "&".join(f"{k}={{{{{by_value[v]}}}}}" if v in by_value else f"{k}={v}" for k, v in pairs)
    return f"{path}?{q}"


def target_from_info(info: dict[str, Any], examples: dict[str, str]) -> Target:
    strategies = [
        {k: v for k, v in s.items() if k != "unique"}
        for s in info.get("strategies", [])
        if s.get("unique") and s.get("by") in _STRATEGY_FIELDS
    ]
    strategies.sort(key=lambda s: STRATEGY_ORDER.index(s["by"]))
    if not strategies:
        raise RecorderError(f"no unique locator for {info.get('role')} {info.get('name')!r}")
    name = info.get("name") or info.get("label") or ""
    tc = next((s for s in strategies if s["by"] == "table_cell"), None)
    if info.get("role") != "cell":
        desc = f'{info.get("role")} "{name}"'
    elif tc and tc["column"].startswith("#"):
        desc = f'value cell of row {tc["row"]["equals"]!r}'
    elif tc:
        desc = f'{tc["column"]!r} cell of row {tc["row"]["column"]}={tc["row"]["equals"]!r}'
    else:
        desc = "table cell"
    return Target.model_validate(
        _templatize({"frame": info["frame"], "description": desc, "strategies": strategies}, examples)
    )


def _auth_boundary(trace: list[TraceEntry]) -> int:
    last_secret = max((i for i, e in enumerate(trace) if e.value and "secret" in e.value), default=-1)
    if last_secret < 0:
        return -1
    for i in range(last_secret + 1, len(trace)):
        if trace[i].action in ("click", "press"):
            return i
    return last_secret


def build_capability(
    spec: GoalSpec,
    result: DiscoveryResult,
    profile: AppProfile,
    tenant: TenantConfig,
    run_id: str,
    redactor: Redactor,
    version: str,
) -> Capability:
    if result.status != "completed" or result.success_info is None:
        raise RecorderError(f"cannot record an unsuccessful run ({result.status}: {result.reason})")
    examples = {i.name: i.example for i in spec.inputs}
    boundary = _auth_boundary(result.trace)
    steps: list[Step] = []
    skipped_human_fills = 0
    for i, e in enumerate(result.trace):
        if e.provenance == "human" and e.action == "fill":
            skipped_human_fills += 1
            continue
        target = target_from_info(e.info, examples)
        changed = {k: v for k, v in e.after.items() if k in e.before and e.before.get(k) != v}
        conditions = [UrlIs(frame=f, path=templatize_location(p, examples)) for f, p in changed.items()]
        irreversible = e.risk == Risk.irreversible
        steps.append(
            Step(
                id=f"s{len(steps) + 1:02d}",
                intent=redactor.text(e.thought)[:160] or f"{e.action} {target.description}",
                action=e.action,  # type: ignore[arg-type]
                target=target,
                value=ValueRef.model_validate(e.value) if e.value else None,
                key=e.key,
                output=e.output,
                risk=Risk.read if e.action == "extract" else e.risk,
                idempotent=not irreversible,
                phase="auth" if i <= boundary else "main",
                expect=Checkpoint(all_of=conditions) if conditions else None,
                provenance=e.provenance,  # type: ignore[arg-type]
            )
        )

    final_main = result.final_locations.get("main")
    success_conditions: list = [ElementPresent(target=target_from_info(result.success_info, examples))]
    if final_main:
        success_conditions.insert(0, UrlIs(frame="main", path=templatize_location(final_main, examples)))

    cap = Capability(
        id=spec.capability_id,
        version=version,
        title=spec.title,
        description=spec.description,
        app=AppRef(product=profile.product, vendor=profile.vendor, versions=[tenant.app_version], surface=profile.surface),
        entry="/login",
        inputs=[InputParam.model_validate(i.model_dump(exclude={"example"})) for i in spec.inputs],
        outputs=spec.outputs,
        outcomes=[
            OutcomeMapping(condition=c.id, code=c.code or c.id.upper(), description=c.description)
            for c in profile.conditions
            if c.klass == "business"
        ],
        steps=steps,
        success=Checkpoint(all_of=success_conditions),
        provenance=Provenance(
            discovery_run=run_id,
            model=result.model,
            recorded_at=datetime.now(timezone.utc),
            tenant=tenant.id,
            goal=redactor.text(spec.goal),
            human_steps=result.human_steps,
        ),
    )
    lint(cap, redactor)
    return cap


def lint(cap: Capability, redactor: Redactor) -> None:
    """The artifact must never contain a secret or a sensitive value observed at runtime."""
    blob = json.dumps(cap.model_dump(mode="json"))
    leaked = [v for v in redactor.literals if len(v) >= 3 and v in blob]
    if leaked:
        raise RecorderError(f"artifact would contain {len(leaked)} sensitive literal(s); refusing to save")
