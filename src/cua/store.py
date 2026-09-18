"""Artifact storage: capabilities/<id>/<semver>.json + capabilities/<id>/overlays/*.yaml.

Plain files in git on purpose: artifacts are reviewed like code (diffs,
PRs, CODEOWNERS for approval), and the content hash binds an approval to
exactly what was reviewed. A registry service would be the next step at
fleet scale; the file layout maps 1:1 onto one.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import ROOT, TenantConfig
from .schema import Approval, Capability, Overlay, apply_overlay

CAPS = ROOT / "capabilities"


def _semver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def versions(cap_id: str, root: Path = CAPS) -> list[str]:
    d = root / cap_id
    return sorted((p.stem for p in d.glob("*.json")), key=_semver) if d.exists() else []


def load(cap_id: str, version: str | None = None, root: Path = CAPS) -> Capability:
    vs = versions(cap_id, root)
    if not vs:
        raise FileNotFoundError(f"no capability {cap_id!r} under {root}")
    v = version or vs[-1]
    return Capability.model_validate_json((root / cap_id / f"{v}.json").read_text())


def save(cap: Capability, root: Path = CAPS) -> Path:
    d = root / cap.id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{cap.version}.json"
    p.write_text(json.dumps(cap.model_dump(mode="json", exclude_none=True), indent=2) + "\n")
    return p


def next_version(cap_id: str, root: Path = CAPS) -> str:
    vs = versions(cap_id, root)
    if not vs:
        return "0.1.0"
    major, minor, _ = _semver(vs[-1])
    return f"{major}.{minor + 1}.0"


def approve(cap: Capability, reviewer: str, notes: str | None = None, root: Path = CAPS) -> Capability:
    cap = cap.model_copy(update={"approval": Approval(
        state="approved", reviewer=reviewer, at=datetime.now(timezone.utc),
        content_hash=cap.content_hash(), notes=notes)})
    save(cap, root)
    return cap


def approval_error(cap: Capability) -> str | None:
    ap = cap.approval
    if ap.state != "approved":
        return f"capability is {ap.state}; approve it (cua approve) or pass --allow-draft"
    if ap.content_hash != cap.content_hash():
        return f"artifact changed after approval ({cap.content_hash()} != {ap.content_hash}); re-review required"
    return None


def overlays(cap_id: str, root: Path = CAPS) -> list[Overlay]:
    d = root / cap_id / "overlays"
    return [Overlay.model_validate(yaml.safe_load(p.read_text())) for p in sorted(d.glob("*.yaml"))] if d.exists() else []


def for_tenant(cap: Capability, tenant: TenantConfig, root: Path = CAPS) -> tuple[Capability, list[str], list[str]]:
    """Specialise a base capability for a tenant: base → app-version overlay → tenant overlay.

    Returns (capability, applied overlay descriptions, warnings)."""
    applied: list[str] = []
    warnings: list[str] = []
    out = cap
    ovs = [o for o in overlays(cap.id, root) if o.applies_to == cap.ref]
    for scope, key in (("app_version", tenant.app_version), ("tenant", tenant.id)):
        for o in ovs:
            if o.scope == scope and o.key == key:
                out = apply_overlay(out, o)
                applied.append(f"{scope}:{key}")
                if not o.reviewed_by:
                    warnings.append(f"overlay {scope}:{key} is not reviewed")
    covered = tenant.app_version in cap.app.versions or any(a.startswith("app_version:") for a in applied)
    if not covered:
        warnings.append(
            f"{cap.ref} was validated on {cap.app.product} {cap.app.versions}; tenant runs {tenant.app_version} "
            "with no overlay — attempting base flow, drift likely"
        )
    return out, applied, warnings
