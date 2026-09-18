from cua.config import load_policy
from cua.policy import Gate, rel_path
from cua.redact import Redactor
from cua.schema import Risk, Sensitivity

BASE = "http://127.0.0.1:8801/t/harbor"
gate = Gate(load_policy(), BASE)


def test_rel_path_scopes_to_tenant():
    assert rel_path(f"{BASE}/member/search?x=1", BASE) == "/member/search?x=1"
    assert rel_path("http://127.0.0.1:8801/t/pineridge/member/search", BASE) is None
    assert rel_path("http://evil.example/t/harbor/login", BASE) is None


def test_links_off_the_allowlist_are_denied():
    link = {"role": "link", "name": "Reports", "href": f"{BASE}/reports"}
    assert gate.check("click", link).verdict == "deny"
    assert gate.check("click", {"role": "link", "name": "x", "href": "https://example.com/"}).verdict == "deny"
    assert gate.check("click", {"role": "link", "name": "Member Inquiry", "href": f"{BASE}/member/search"}).verdict == "allow"


def test_irreversible_controls_need_approval():
    btn = {"role": "button", "name": "Confirm & Open", "formAction": f"{BASE}/share/open", "formMethod": "post"}
    d = gate.check("click", btn)
    assert (d.verdict, d.risk) == ("needs_approval", Risk.irreversible)
    # same POST under a harmless label is still caught by the form-target rule
    assert gate.check("click", btn | {"name": "OK"}).verdict == "needs_approval"


def test_recorded_risk_can_raise_but_never_lower_live_risk():
    benign = {"role": "button", "name": "Continue", "formAction": f"{BASE}/share/review", "formMethod": "get"}
    assert gate.check("click", benign).verdict == "allow"
    assert gate.check("click", benign, recorded_risk=Risk.irreversible).verdict == "needs_approval"


def test_unknown_action_type_denied():
    assert gate.check("drag", {"role": "button"}).verdict == "deny"


def test_redaction_layers():
    r = Redactor()
    r.register_secret("Harbor#Demo2026")
    r.register("member_id", "10042", Sensitivity.internal)
    r.register("member_name", "Dana Whitfield", Sensitivity.pii)
    out = r.text("login Harbor#Demo2026 member 10042 Dana Whitfield SSN 123-45-6789 bal $12,403.22 dob **/**/1981 a@b.com")
    for leaked in ("Harbor#Demo2026", "10042", "Dana Whitfield", "123-45-6789", "12,403.22", "1981", "a@b.com"):
        assert leaked not in out
    assert "[member_id:…42]" in out and "[PII:member_name]" in out and "[SECRET]" in out


def test_redaction_does_not_eat_capability_refs():
    assert Redactor().text("member.savings_balance.read@0.1.0") == "member.savings_balance.read@0.1.0"
