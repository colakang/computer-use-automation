"""Environment knowledge: app profiles, tenants, policy, secrets.

Separation of concerns across the multi-tenant axis:

    AppProfile  (per vendor product)   runtime conditions, dialogs — shared by all tenants
    Tenant      (per institution)      base URL, vendor version, secret references
    Policy      (per deployment)       what the automation may do at all
    Capability  (per flow)             the contract + recorded body
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field

from .schema import Strict, Target

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"


class Match(Strict):
    text_contains: str | None = None
    text_regex: str | None = None
    frame: str | None = Field(None, description="Restrict to a frame; None = any frame.")

    def hit(self, frame_texts: dict[str, str]) -> str | None:
        for name, text in frame_texts.items():
            if self.frame and name != self.frame and name != "*":  # "*": surface without frames
                continue
            if self.text_contains and self.text_contains.lower() in text.lower():
                return name
            if self.text_regex and re.search(self.text_regex, text):
                return name
        return None


class Dismiss(Strict):
    type: Literal["dismiss"] = "dismiss"
    target: Target


class Reauthenticate(Strict):
    type: Literal["reauthenticate"] = "reauthenticate"


class Reload(Strict):
    """Re-issue the load of the frame that shows the condition — only allowed
    when the step that caused the load is idempotent (never after a commit)."""

    type: Literal["reload"] = "reload"
    backoff_ms: int = 1000


Handler = Annotated[Dismiss | Reauthenticate | Reload, Field(discriminator="type")]


class ConditionSpec(Strict):
    """A runtime state the app can be in, independent of which flow is running.

    ``class`` is the taxonomy the brief asks for:
      business    — a legitimate answer for the caller (terminal, not an error)
      recoverable — handled in place, then the step is retried
      fatal       — stop, surface a debuggable failure (optionally escalate)
    """

    id: str
    klass: Literal["business", "recoverable", "fatal"] = Field(alias="class")
    match: Match
    description: str
    code: str | None = None          # business: default outcome code
    kind: str | None = None          # fatal: failure kind
    handler: Handler | None = None   # recoverable: what to do
    max_attempts: int = 2


class DialogRule(Strict):
    match: str
    action: Literal["accept", "dismiss"]
    description: str


class AppProfile(Strict):
    product: str
    vendor: str
    surface: Literal["web", "legacy_web", "desktop"]
    conditions: list[ConditionSpec]
    dialogs: list[DialogRule] = []


class TenantConfig(Strict):
    id: str
    display: str
    app: str
    app_version: str
    base_url: str
    secrets: dict[str, str] = Field(description="secret name -> reference, e.g. env:HARBOR_TELLER_PASSWORD")


class ActionRule(Strict):
    role: str | None = None
    name_regex: str | None = None
    form_method: str | None = None
    path_regex: str | None = None


class Policy(Strict):
    """What the automation is allowed to do, independent of any artifact.

    Enforced at one choke point (``policy.Gate``) used by both discovery and
    replay, plus a network-level route filter in the browser as a backstop.
    """

    allowed_actions: list[Literal["click", "fill", "select", "press", "extract"]]
    allowed_paths: list[str] = Field(description="Regexes on path relative to the tenant base URL.")
    denied_paths: list[str] = []
    irreversible: list[ActionRule] = Field(description="Controls whose activation commits a change.")
    max_discovery_steps: int = 30


def _load_yaml(p: Path) -> dict:
    return yaml.safe_load(p.read_text())


def load_profile(app: str) -> AppProfile:
    return AppProfile.model_validate(_load_yaml(CONFIG / "apps" / f"{app}.yaml"))


def load_tenant(tenant: str) -> TenantConfig:
    return TenantConfig.model_validate(_load_yaml(CONFIG / "tenants" / f"{tenant}.yaml"))


def load_policy() -> Policy:
    return Policy.model_validate(_load_yaml(CONFIG / "policy.yaml"))


class SecretStore:
    """Resolves ``env:NAME`` references. The only place plaintext credentials
    exist in-process; values are registered with the redactor on read so they
    can never be logged, and they are never shown to the model."""

    def __init__(self, tenant: TenantConfig, on_read=None) -> None:
        self._refs = tenant.secrets
        self._on_read = on_read

    def names(self) -> list[str]:
        return sorted(self._refs)

    def get(self, name: str) -> str:
        ref = self._refs.get(name)
        if ref is None:
            raise KeyError(f"unknown secret {name!r}")
        scheme, _, key = ref.partition(":")
        if scheme != "env":
            raise ValueError(f"unsupported secret scheme {scheme!r}")
        val = os.environ.get(key)
        if not val:
            raise KeyError(f"secret {name!r} not set (env {key})")
        if self._on_read:
            self._on_read(val)
        return val
