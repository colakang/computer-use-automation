import pytest
from pydantic import ValidationError

from cua import store
from cua.schema import Capability, Overlay, Step, apply_overlay, location_matches, parse_output, render
from cua.schema import OutputField, ParamType

BAL = "member.savings_balance.read"


def test_committed_artifacts_validate_and_roundtrip():
    for cid in (BAL, "member.share.open"):
        cap = store.load(cid)
        again = Capability.model_validate_json(cap.model_dump_json())
        assert again.content_hash() == cap.content_hash()
        assert store.approval_error(cap) is None, "committed artifacts are approved and untampered"


def test_every_target_has_a_semantic_strategy_first():
    for cid in (BAL, "member.share.open"):
        for s in store.load(cid).steps:
            assert s.target.strategies[0].by in ("role_name", "label", "table_cell"), s.id
            assert s.target.strategies[-1].by == "css", "structural path kept only as last resort"


def test_editing_an_approved_artifact_voids_the_approval():
    cap = store.load(BAL)
    edited = cap.model_copy(deep=True)
    edited.steps[0].timeout_ms += 1
    assert store.approval_error(edited) and "changed after approval" in store.approval_error(edited)


def test_undeclared_template_is_rejected():
    data = store.load(BAL).model_dump(mode="json")
    data["steps"][6]["target"]["strategies"][0]["name"] = "{{account_id}}"
    with pytest.raises(ValidationError, match="not a declared input"):
        Capability.model_validate(data)


def test_irreversible_step_cannot_claim_idempotence():
    step = store.load("member.share.open").steps[9].model_dump()
    assert step["risk"] == "irreversible"
    step["idempotent"] = True
    with pytest.raises(ValidationError, match="cannot be idempotent"):
        Step.model_validate(step)


def test_output_must_be_extracted():
    data = store.load(BAL).model_dump(mode="json")
    data["steps"] = [s for s in data["steps"] if s.get("output") != "member_name"]
    with pytest.raises(ValidationError, match="never extracted"):
        Capability.model_validate(data)


def test_overlay_retargets_without_changing_contract():
    cap = store.load(BAL)
    ov = store.overlays(BAL)[0]
    out = apply_overlay(cap, ov)
    assert out.tool_schema() == cap.tool_schema()
    assert [s.id for s in out.steps] == [s.id for s in cap.steps]
    assert out.steps[5].target.strategies[0].name == "Find"


def test_overlay_for_other_version_is_refused():
    ov = store.overlays(BAL)[0].model_copy(update={"applies_to": f"{BAL}@9.9.9"})
    with pytest.raises(ValueError):
        apply_overlay(store.load(BAL), ov)


def test_tool_schema_is_function_calling_shaped():
    ts = store.load(BAL).tool_schema()
    assert ts["name"] == "member__savings_balance__read"
    assert ts["input_schema"]["required"] == ["member_id"]
    assert ts["input_schema"]["properties"]["member_id"]["pattern"]


def test_money_parse_and_location_matching():
    f = OutputField(name="x", type=ParamType.money, description="")
    assert parse_output(f, "$12,403.22") == {"amount": "12403.22", "currency": "USD"}
    assert parse_output(f, "($5.00)")["amount"] == "-5.00"
    assert location_matches("/r?a=Holiday+Club&b=1", render("/r?a={{t}}&b=1", {"t": "Holiday Club"}))
    assert not location_matches("/r?a=1", "/r?a=2")
