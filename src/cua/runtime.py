"""Wiring: one live session = tenant + profile + policy + surface + evidence + control channel."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from dotenv import load_dotenv

from .config import ROOT, AppProfile, Policy, SecretStore, TenantConfig, load_policy, load_profile, load_tenant
from .evidence import RunLog
from .handoff import ControlChannel
from .policy import Gate
from .redact import Redactor
from .surface.web import WebSurface

load_dotenv(ROOT / ".env")


@dataclass
class Session:
    tenant: TenantConfig
    profile: AppProfile
    policy: Policy
    gate: Gate
    redactor: Redactor
    log: RunLog
    surface: WebSurface
    secrets: SecretStore
    control: ControlChannel | None

    def close(self) -> None:
        if self.control:
            self.control.shutdown()
        self.surface.close()
        self.log.close()


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def ensure_mock(base_url: str) -> None:
    """Start the mock app in-process if the tenant points at it and nothing is listening."""
    u = urlsplit(base_url)
    if u.hostname not in ("127.0.0.1", "localhost") or _port_open(u.hostname, u.port or 80):
        return
    import uvicorn

    from mockbank.app import app

    server = uvicorn.Server(uvicorn.Config(app, host=u.hostname, port=u.port or 80, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if _port_open(u.hostname, u.port or 80):
            return
        time.sleep(0.05)
    raise RuntimeError("mock app did not start")


def set_faults(base_url: str, faults: dict) -> None:
    """Harness-only: configure the mock's fault injection (resets anything not given)."""
    u = urlsplit(base_url)
    httpx.post(f"{u.scheme}://{u.netloc}/__control/faults", json=faults, timeout=5).raise_for_status()


def open_session(kind: str, slug: str, tenant_id: str, *, evidence_root: Path, headed: bool = False,
                 escalate: bool = False, console_port: int = 8802, surface: str = "web") -> Session:
    tenant = load_tenant(tenant_id)
    profile = load_profile(tenant.app)
    policy = load_policy()
    gate = Gate(policy, tenant.base_url)
    redactor = Redactor()
    log = RunLog(evidence_root, kind, slug, redactor)
    ensure_mock(tenant.base_url)
    kind_cls = WebSurface
    if surface == "vision":
        from .surface.vision import VisionSurface

        kind_cls = VisionSurface
    surface_obj = kind_cls(tenant.base_url, gate, headed=headed, dialog_rules=profile.dialogs,
                           on_event=lambda e, f: log.event(e, **f))
    surface = surface_obj  # noqa: F841 (name kept for readability below)
    secrets = SecretStore(tenant, on_read=redactor.register_secret)
    for name in secrets.names():  # register up front so a secret can never be logged, even before first use
        try:
            secrets.get(name)
        except KeyError:
            pass
    control = None
    if escalate:
        control = ControlChannel(surface, log, queue_dir=ROOT / "interventions",
                                 console_port=console_port)
    log.event("session.open", kind=kind, tenant=tenant.id, app=f"{profile.product}@{tenant.app_version}",
              base_url=tenant.base_url, headed=headed, escalation=bool(control), surface=type(surface_obj).__name__)
    return Session(tenant, profile, policy, gate, redactor, log, surface, secrets, control)
