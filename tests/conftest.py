from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("HARBOR_TELLER_USER", "teller01")
os.environ.setdefault("HARBOR_TELLER_PASSWORD", "Harbor#Demo2026")
os.environ.setdefault("PINERIDGE_TELLER_USER", "teller01")
os.environ.setdefault("PINERIDGE_TELLER_PASSWORD", "Harbor#Demo2026")

from cua import store  # noqa: E402
from cua.config import load_tenant  # noqa: E402
from cua.replay import ReplayOptions, ReplayResult, Replayer  # noqa: E402
from cua.runtime import ensure_mock, open_session, set_faults  # noqa: E402

BAL = "member.savings_balance.read"
SHARE = "member.share.open"


@pytest.fixture(scope="session", autouse=True)
def mock_app():
    ensure_mock(load_tenant("harbor").base_url)


@pytest.fixture
def replay(tmp_path: Path):
    def _run(cap_id: str, inputs: dict, *, tenant: str = "harbor", faults: dict | None = None,
             overlays: bool = True, cap=None, **opts) -> ReplayResult:
        base = cap or store.load(cap_id)
        tconf = load_tenant(tenant)
        spec, _, _ = store.for_tenant(base, tconf) if overlays else (base, [], [])
        sess = open_session("replay", "test", tenant, evidence_root=tmp_path)
        try:
            set_faults(tconf.base_url, faults or {})
            r = Replayer(spec, sess.profile, tenant, sess.surface, sess.gate, sess.log, sess.redactor,
                         sess.secrets, sess.control, ReplayOptions(**opts), approval_error=store.approval_error(base))
            return r.run(inputs)
        finally:
            sess.close()
            set_faults(tconf.base_url, {})

    return _run
