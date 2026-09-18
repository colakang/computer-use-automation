"""Discovery loop + recorder + handoff, with a rule-based fake model (no network)."""

import re
import threading
import time

import httpx

from cua.agent import DiscoveryAgent
from cua.llm import Scripted
from cua.recorder import build_capability
from cua.replay import ReplayOptions, Replayer
from cua.runtime import open_session, set_faults
from cua.schema import GoalSpec

SPEC = GoalSpec.model_validate({
    "capability_id": "test.balance.read", "title": "t", "description": "t", "tenant": "harbor",
    "goal": "look up member {member_id} and read savings balance",
    "inputs": [{"name": "member_id", "type": "string", "description": "id", "pattern": r"^\d{5}$", "example": "10388"}],
    "outputs": [{"name": "savings_balance", "type": "money", "description": "bal"}],
})


def ref(pattern: str):
    """Fake model step: find the first ref on the line matching `pattern` in the observation."""
    def pick(prompt: str) -> str:
        obs = prompt.split("OBSERVATION:", 1)[1]
        for line in obs.splitlines():
            if re.search(pattern, line):
                return re.findall(r"\[(\w+:e\d+)\]", line)
        raise AssertionError(f"no line matches {pattern}")
    return pick


def cell_in_row(row_pat: str, col: int):
    return lambda p: ref(row_pat)(p)[col]


def script():
    return Scripted([
        lambda p: {"thought": "user", "action": "fill", "ref": ref(r'label="User ID"')(p)[0], "secret": "username"},
        lambda p: {"thought": "pw", "action": "fill", "ref": ref(r'label="Password"')(p)[0], "secret": "password"},
        lambda p: {"thought": "go", "action": "click", "ref": ref(r'button "Sign On"')(p)[0]},
        lambda p: {"thought": "nav", "action": "click", "ref": ref(r'link "Member Inquiry"')(p)[0]},
        lambda p: {"thought": "id", "action": "fill", "ref": ref(r'label="Search Value:"')(p)[0], "text": "10388"},
        lambda p: {"thought": "s", "action": "click", "ref": ref(r'button "Search"')(p)[0]},
        lambda p: {"thought": "open", "action": "click", "ref": ref(r'link "10388"')(p)[0]},
        lambda p: {"thought": "read", "action": "extract", "ref": cell_in_row("Share Savings", 3)(p), "output": "savings_balance"},
        lambda p: {"thought": "done", "action": "done", "success_ref": cell_in_row("Share Savings", 3)(p)},
    ])


def test_discovery_records_a_parameterized_replayable_artifact(tmp_path):
    sess = open_session("discovery", "test", "harbor", evidence_root=tmp_path)
    set_faults(sess.tenant.base_url, {})
    try:
        sess.surface.goto("/login")
        agent = DiscoveryAgent(SPEC, sess.surface, sess.gate, script(), sess.log, None, sess.secrets.names(), sess.secrets.get)
        res = agent.run()
        assert res.status == "completed", res.reason
        cap = build_capability(SPEC, res, sess.profile, sess.tenant, sess.log.run_id, sess.redactor, "0.1.0")
    finally:
        sess.close()
    link = cap.steps[6]
    assert link.target.strategies[0].name == "{{member_id}}", "clicked value parameterized"
    assert cap.steps[4].value.input == "member_id", "literal equal to an input becomes an input binding"
    assert [s.phase for s in cap.steps[:3]] == ["auth"] * 3
    assert "10388" not in cap.model_dump_json() and "Harbor#Demo2026" not in cap.model_dump_json()

    # ...and it replays for a *different* member without a model
    sess = open_session("replay", "test", "harbor", evidence_root=tmp_path)
    try:
        r = Replayer(cap, sess.profile, "harbor", sess.surface, sess.gate, sess.log, sess.redactor, sess.secrets,
                     None, ReplayOptions(allow_draft=True)).run({"member_id": "10042"})
    finally:
        sess.close()
    assert r.status == "success" and r.outputs["savings_balance"]["amount"] == "12403.22"


def test_policy_denial_is_fed_back_and_stuck_escalates(tmp_path):
    reports = lambda p: {"thought": "reports", "action": "click", "ref": ref(r'link "Reports"')(p)[0]}  # noqa: E731
    login = script()._replies[:3]
    llm = Scripted(login + [reports] * 4)
    sess = open_session("discovery", "test", "harbor", evidence_root=tmp_path)
    try:
        sess.surface.goto("/login")
        res = DiscoveryAgent(SPEC, sess.surface, sess.gate, llm, sess.log, None, sess.secrets.names(), sess.secrets.get).run()
    finally:
        sess.close()
    assert res.status == "failed" and "stuck" in res.reason
    assert any("DENIED by policy" in p for p in llm.prompts), "denial explained to the model"
    assert not sess.surface.blocked_requests, "the gate stopped it before the browser tried"


def test_live_session_handoff_capture_and_resume(tmp_path):
    """Replay fails on an unknown modal → operator claims the SAME session via the console API,
    clicks the popup away, hands back with 'retry' → replay completes; human action is recorded."""
    from conftest import BAL
    from cua import store

    cap = store.load(BAL)
    sess = open_session("replay", "test", "harbor", evidence_root=tmp_path, escalate=True, console_port=8812)
    set_faults(sess.tenant.base_url, {"survey_modal": True})

    def operator():
        c = "http://127.0.0.1:8812"
        for _ in range(200):
            try:
                if (httpx.get(f"{c}/api/state", timeout=1).json().get("intervention") or {}).get("status") == "open":
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        # console input before claiming is ignored: control must be taken first
        httpx.post(f"{c}/api/click", json={"x": 562, "y": 190})
        time.sleep(0.5)
        httpx.post(f"{c}/api/claim", json={"operator": "op:test"})
        time.sleep(0.5)
        httpx.post(f"{c}/api/click", json={"x": 562, "y": 190})
        time.sleep(0.8)
        httpx.post(f"{c}/api/release", json={"operator": "op:test", "resolution": "retry"})

    t = threading.Thread(target=operator)
    t.start()
    try:
        r = Replayer(cap, sess.profile, "harbor", sess.surface, sess.gate, sess.log, sess.redactor, sess.secrets,
                     sess.control, ReplayOptions(on_failure="escalate"), approval_error=store.approval_error(cap)).run({"member_id": "10042"})
        transitions = [(x["from"], x["to"]) for x in sess.control.transitions]
    finally:
        t.join()
        sess.close()
        set_faults(sess.tenant.base_url, {})
    assert r.status == "success"
    iv = r.interventions[0]
    assert iv["resolution"] == "retry" and iv["by"] == "op:test"
    assert [a["name"] for a in iv["human_actions"]] == ["No thanks"]
    assert transitions == [("automation", "awaiting_human"), ("awaiting_human", "human"), ("human", "automation")]
