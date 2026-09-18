"""Integration: real browser, real mock app, committed artifacts, no LLM."""

import json

import pytest

from conftest import BAL, SHARE

OK = {"member_id": "10042"}
SHARE_IN = {"member_id": "10042", "share_type": "Money Market", "nickname": "Test", "opening_deposit": "10.00"}


def test_happy_path_returns_typed_outputs(replay):
    r = replay(BAL, OK)
    assert r.status == "success"
    assert r.outputs == {"savings_balance": {"amount": "12403.22", "currency": "USD"}, "member_name": "Dana Whitfield"}
    assert not r.recoveries and not r.drift


def test_other_member_same_artifact(replay):
    r = replay(BAL, {"member_id": "10388"})
    assert r.outputs["savings_balance"]["amount"] == "88120.40", "row found by meaning, not by position"


def test_not_found_is_a_business_outcome(replay):
    r = replay(BAL, {"member_id": "99999"})
    assert r.status == "business_outcome" and r.outcome.code == "RECORD_NOT_FOUND" and r.failure is None


def test_bad_input_rejected_before_ui(replay):
    r = replay(BAL, {"member_id": "12ab"})
    assert r.status == "rejected" and r.failure.kind == "invalid_input" and r.steps_executed == 0
    assert replay(BAL, {"member_id": "10042", "extra": "x"}).failure.kind == "invalid_input"


@pytest.mark.parametrize("faults,condition", [
    ({"interstitial": True}, "system_notice"),
    ({"expire_after": 1}, "session_expired"),
    ({"transient_503": 1}, "host_unavailable"),
    ({"hold_dialog": True}, "native_dialog"),
])
def test_recoverable_conditions(replay, faults, condition):
    r = replay(BAL, OK, faults=faults)
    assert r.status == "success", r.failure
    assert condition in [x["condition"] for x in r.recoveries]


def test_recovery_is_bounded(replay):
    r = replay(BAL, OK, faults={"transient_503": 5})
    assert r.status == "failure" and r.failure.kind == "recovery_exhausted"


@pytest.mark.parametrize("faults,kind", [
    ({"error_500_on": "/member/detail"}, "app_error"),
    ({"deny_detail": True}, "permission_denied"),
    ({"survey_modal": True}, "action_blocked"),
])
def test_hard_failures_are_classified_with_evidence(replay, faults, kind):
    r = replay(BAL, OK, faults=faults)
    assert r.status == "failure" and r.failure.kind == kind
    assert r.failure.step and r.failure.expected and r.failure.observed
    assert any(e.endswith(".png") for e in r.failure.evidence)


def test_slow_load_is_waited_for_not_slept(replay):
    r = replay(BAL, OK, faults={"slow_ms": 2500})
    assert r.status == "success" and r.duration_ms > 5000


def test_draft_or_tampered_artifacts_do_not_run_unattended(replay):
    from cua import store

    cap = store.load(BAL)
    edited = cap.model_copy(deep=True)
    edited.steps[3].timeout_ms = 9999
    r = replay(BAL, OK, cap=edited)
    assert r.status == "rejected" and r.failure.kind == "not_approved"
    assert replay(BAL, OK, cap=edited, allow_draft=True).status == "success"


def test_cross_tenant_without_overlay_refuses_positional_read(replay):
    r = replay(BAL, OK, tenant="pineridge", overlays=False)
    assert r.status == "failure" and r.failure.kind == "target_drifted"
    assert r.drift, "fallback use on navigation steps is reported as drift"


def test_cross_tenant_with_overlay(replay):
    r = replay(BAL, OK, tenant="pineridge")
    assert r.status == "success" and not r.drift
    assert r.outputs["savings_balance"]["amount"] == "12403.22"


def test_irreversible_blocked_without_authorization(replay):
    r = replay(SHARE, SHARE_IN)
    assert r.status == "failure" and r.failure.kind == "approval_required" and r.failure.step == "s10"


def test_irreversible_with_authorization_commits(replay):
    r = replay(SHARE, SHARE_IN, irreversible="authorized")
    assert r.status == "success" and r.outputs["confirmation_number"].startswith("CNF-")


def test_write_flow_validation_is_business_outcome(replay):
    r = replay(SHARE, SHARE_IN | {"member_id": "55555"}, irreversible="authorized")
    assert r.status == "business_outcome" and r.outcome.code == "VALIDATION_ERROR"


def test_nothing_sensitive_is_persisted(replay, tmp_path):
    replay(BAL, OK)
    replay(BAL, OK, faults={"error_500_on": "/member/detail"})
    blob = "".join(p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.suffix in (".jsonl", ".json", ".txt"))
    for leaked in ("Harbor#Demo2026", "Dana Whitfield", "12,403.22", "12403.22", "4417", "10042"):
        assert leaked not in blob, leaked
