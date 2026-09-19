"""cua — command line.

    cua mock                                   run the CoreOne Teller mock
    cua discover goals/savings_balance.yaml    LLM discovery → draft capability
    cua show <capability>                      human-readable review + tool schema
    cua approve <capability> --reviewer NAME   bind approval to content hash
    cua replay <capability> -t harbor -i member_id=10042 [--inject slow_ms=3000]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml

from . import store
from .config import ROOT, load_tenant
from .schema import GoalSpec

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Computer-use automation: discover → artifact → replay.")

EvidenceOpt = Annotated[Path, typer.Option("--evidence", help="Where run evidence is written.")]


def _kv(pairs: list[str]) -> dict[str, str]:
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep:
            raise typer.BadParameter(f"expected key=value, got {p!r}")
        out[k] = v
    return out


def _fault_value(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        return v


@app.command()
def mock(port: int = 8801) -> None:
    """Run the CoreOne Teller mock app (two tenants, fault injection)."""
    import uvicorn

    uvicorn.run("mockbank.app:app", host="127.0.0.1", port=port, log_level="warning")


@app.command()
def discover(
    goal_file: Path,
    tenant: Annotated[Optional[str], typer.Option("-t", "--tenant")] = None,
    llm: Annotated[Optional[str], typer.Option(help="claude-cli | anthropic")] = None,
    model: Optional[str] = None,
    headed: bool = False,
    escalate: Annotated[bool, typer.Option(help="Route interventions to the operator console.")] = True,
    evidence: EvidenceOpt = ROOT / "runs",
) -> None:
    """Run an LLM-driven discovery for a goal spec and record a draft capability."""
    from .agent import DiscoveryAgent
    from .llm import make_llm
    from .recorder import RecorderError, build_capability
    from .runtime import open_session, set_faults

    spec = GoalSpec.model_validate(yaml.safe_load(goal_file.read_text()))
    tenant_id = tenant or spec.tenant
    brain = make_llm(llm, model)
    sess = open_session("discovery", spec.capability_id.replace(".", "-"), tenant_id,
                        evidence_root=evidence, headed=headed, escalate=escalate)
    set_faults(sess.tenant.base_url, {})
    for i in spec.inputs:
        sess.redactor.register(i.name, i.example, i.sensitivity)
    log = sess.log
    log.event("discovery.start", capability=spec.capability_id, goal=spec.rendered_goal(), llm=brain.name,
              inputs=[i.name for i in spec.inputs], outputs=[o.name for o in spec.outputs])
    typer.echo(f"run {log.run_id}\nevidence {log.dir}")
    try:
        sess.surface.goto("/login")
        agent = DiscoveryAgent(spec, sess.surface, sess.gate, brain, log, sess.control,
                               sess.secrets.names(), sess.secrets.get)
        result = agent.run()
        sess.surface.screenshot(log.next_shot(f"final-{result.status}"), sess.redactor.literals)
        log.event("discovery.end", status=result.status, reason=result.reason, steps=result.steps_used,
                  actions=len(result.trace), human_steps=result.human_steps, model=result.model)
        if result.status != "completed":
            log.write_text("final-snapshot.txt", sess.surface.observe(opaque=True).text)
            typer.echo(f"discovery {result.status}: {result.reason}", err=True)
            raise typer.Exit(2)
        try:
            cap = build_capability(spec, result, sess.profile, sess.tenant, log.run_id, sess.redactor,
                                   store.next_version(spec.capability_id))
        except RecorderError as e:
            log.event("record.error", error=str(e))
            typer.echo(f"recording failed: {e}", err=True)
            raise typer.Exit(3)
        path = store.save(cap)
        log.write_json("artifact.json", cap.model_dump(mode="json", exclude_none=True), redact=False)
        log.event("record.saved", path=str(path.relative_to(ROOT)), capability=cap.ref, steps=len(cap.steps),
                  content_hash=cap.content_hash())
        typer.echo(f"saved {cap.ref} → {path.relative_to(ROOT)} (draft; review with `cua show`, then `cua approve`)")
    finally:
        sess.close()


@app.command()
def show(capability: str, version: Optional[str] = None, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Review view of a capability: contract, steps, locators, risk, approval."""
    cap = store.load(capability, version)
    if as_json:
        typer.echo(json.dumps(cap.model_dump(mode="json", exclude_none=True), indent=2))
        return
    ap = cap.approval
    typer.echo(f"{cap.ref} — {cap.title}  [{ap.state}{' by ' + ap.reviewer if ap.reviewer else ''}]")
    typer.echo(f"  {cap.description}")
    typer.echo(f"  app: {cap.app.product} {cap.app.versions} ({cap.app.surface})   max risk: {cap.max_risk.value}")
    typer.echo(f"  hash: {cap.content_hash()}   recorded from {cap.provenance.discovery_run} by {cap.provenance.model}")
    typer.echo("\n  inputs:")
    for i in cap.inputs:
        typer.echo(f"    {i.name}: {i.type.value}{' /' + i.pattern + '/' if i.pattern else ''}  ({i.sensitivity.value}) — {i.description}")
    typer.echo("  outputs:")
    for o in cap.outputs:
        typer.echo(f"    {o.name}: {o.type.value}  ({o.sensitivity.value}) — {o.description}")
    typer.echo("  business outcomes:")
    for o in cap.outcomes:
        typer.echo(f"    {o.code:<18} ← {o.condition}: {o.description}")
    typer.echo("\n  steps:")
    for s in cap.steps:
        v = ""
        if s.value:
            v = f" ← {'input:' + s.value.input if s.value.input else 'secret:' + s.value.secret if s.value.secret else repr(s.value.literal)}"
        if s.output:
            v = f" → {s.output}"
        flags = f"[{s.phase}/{s.risk.value}{'' if s.idempotent else '/non-idempotent'}{'/HUMAN' if s.provenance == 'human' else ''}]"
        typer.echo(f"    {s.id} {flags:<28} {s.action:<7} {s.target.description} @{s.target.frame}{v}")
        typer.echo(f"         intent: {s.intent}")
        for n, st in enumerate(s.target.strategies):
            d = st.model_dump(exclude={"by"})
            typer.echo(f"         {'→' if n == 0 else ' '} {st.by}: {json.dumps(d)}")
        if s.expect:
            typer.echo(f"         expect: {[c.model_dump(exclude={'kind'}) if c.kind != 'element' else 'element ' + c.target.description for c in s.expect.all_of]}")
    typer.echo("\n  tool schema (for calling agents):")
    typer.echo("    " + json.dumps(cap.tool_schema(), indent=2).replace("\n", "\n    "))
    ovs = store.overlays(cap.id)
    if ovs:
        typer.echo("\n  overlays:")
        for o in ovs:
            typer.echo(f"    {o.scope}:{o.key} → {o.applies_to} ({len(o.targets)} target patches) — {o.reason} [reviewed_by={o.reviewed_by}]")


@app.command()
def approve(capability: str, reviewer: Annotated[str, typer.Option()], version: Optional[str] = None,
            notes: Optional[str] = None) -> None:
    """Approve a draft for unattended replay (binds approval to the current content hash)."""
    cap = store.approve(store.load(capability, version), reviewer, notes)
    typer.echo(f"approved {cap.ref} ({cap.approval.content_hash}) by {reviewer}")


@app.command()
def replay(
    capability: str,
    tenant: Annotated[str, typer.Option("-t", "--tenant")],
    input: Annotated[list[str], typer.Option("-i", "--input", help="name=value")] = [],
    version: Optional[str] = None,
    inject: Annotated[list[str], typer.Option(help="mock fault, e.g. slow_ms=3000, interstitial=true")] = [],
    allow_draft: bool = False,
    irreversible: Annotated[str, typer.Option(help="block | escalate | authorized")] = "block",
    on_failure: Annotated[str, typer.Option(help="fail | escalate")] = "fail",
    no_overlays: Annotated[bool, typer.Option(help="Replay the base artifact even if overlays exist.")] = False,
    headed: bool = False,
    surface: Annotated[str, typer.Option(help="web (DOM) | vision (pixels only, prototype)")] = "web",
    evidence: EvidenceOpt = ROOT / "runs",
) -> None:
    """Replay a capability deterministically (no LLM). Prints the structured result as JSON."""
    from .replay import ReplayOptions, Replayer
    from .runtime import open_session, set_faults

    base = store.load(capability, version)
    tconf = load_tenant(tenant)
    cap, applied, warnings = (base, [], []) if no_overlays else store.for_tenant(base, tconf)
    approval_err = store.approval_error(base)
    if not approval_err and warnings and any("not reviewed" in w for w in warnings):
        approval_err = "; ".join(w for w in warnings if "not reviewed" in w)
    escalate = irreversible == "escalate" or on_failure == "escalate"
    sess = open_session("replay", capability.replace(".", "-") + ("-vision" if surface == "vision" else ""), tenant,
                        evidence_root=evidence, headed=headed, escalate=escalate, surface=surface)
    try:
        faults = {k: _fault_value(v) for k, v in _kv(inject).items()}
        set_faults(sess.tenant.base_url, faults)
        sess.log.event("replay.config", capability=base.ref, overlays=applied, warnings=warnings, injected_faults=faults)
        for w in warnings:
            typer.echo(f"warning: {w}", err=True)
        r = Replayer(cap, sess.profile, tenant, sess.surface, sess.gate, sess.log, sess.redactor, sess.secrets,
                     sess.control, ReplayOptions(allow_draft=allow_draft, irreversible=irreversible,  # type: ignore[arg-type]
                                                 on_failure=on_failure), approval_error=approval_err)
        result = r.run(_kv(input))
        out = result.model_dump(mode="json")
        out["overlays"] = applied
        out["evidence"] = str(sess.log.dir.relative_to(ROOT)) if sess.log.dir.is_relative_to(ROOT) else str(sess.log.dir)
        # stdout is the caller channel: it carries the real outputs. Everything persisted is redacted.
        typer.echo(json.dumps(out, indent=2))
    finally:
        sess.close()
    sys.exit({"success": 0, "business_outcome": 0, "failure": 1, "rejected": 2}[result.status])


if __name__ == "__main__":
    app()
