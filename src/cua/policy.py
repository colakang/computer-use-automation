"""Single choke point for "may the automation do this?".

Both the discovery agent and the replay engine call :meth:`Gate.check`
before every action; there is no code path that acts on the surface without
it. The browser additionally runs a route filter (:func:`route_guard`) so a
navigation that the gate could not foresee (a script redirect, a form posting
somewhere unexpected) is still blocked at the network layer.

Risk handling decision: irreversible actions are never silently allowed.
  * discovery → pause and ask a human (the model proposed it; a person decides),
  * replay    → require explicit per-invocation authorization from the caller;
                without it, escalate to a human (or block if no operator).
Blocking outright would make "open a share account" impossible to automate;
flag-only would let a mis-recorded step commit a change unattended.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from .config import Policy
from .schema import Risk

Verdict = Literal["allow", "deny", "needs_approval"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    risk: Risk
    reason: str


def rel_path(url: str, base_url: str) -> str | None:
    """Path+query relative to the tenant base, or None if outside it."""
    u, b = urlsplit(url), urlsplit(base_url)
    if (u.scheme, u.netloc) != (b.scheme, b.netloc):
        return None
    base_path = b.path.rstrip("/")
    if not (u.path == base_path or u.path.startswith(base_path + "/")):
        return None
    rel = u.path[len(base_path):] or "/"
    return rel + (f"?{u.query}" if u.query else "")


class Gate:
    def __init__(self, policy: Policy, base_url: str) -> None:
        self.policy = policy
        self.base_url = base_url

    def path_allowed(self, url: str) -> tuple[bool, str]:
        rel = rel_path(url, self.base_url)
        if rel is None:
            return False, f"outside tenant origin/base: {url}"
        path = rel.split("?", 1)[0]
        if any(re.search(p, path) for p in self.policy.denied_paths):
            return False, f"path {path} is explicitly denied"
        if not any(re.search(p, path) for p in self.policy.allowed_paths):
            return False, f"path {path} is not on the allowlist"
        return True, "ok"

    def classify(self, action: str, el: dict[str, Any]) -> Risk:
        if action == "extract":
            return Risk.read
        if action != "click" and not (action == "press" and el.get("formMethod")):
            return Risk.reversible
        form_rel = rel_path(el["formAction"], self.base_url) if el.get("formAction") else None
        for rule in self.policy.irreversible:
            if rule.role and rule.role != el.get("role"):
                continue
            if rule.name_regex and not re.search(rule.name_regex, el.get("name") or ""):
                continue
            if rule.form_method and rule.form_method != el.get("formMethod"):
                continue
            if rule.path_regex and not (form_rel and re.search(rule.path_regex, form_rel)):
                continue
            return Risk.irreversible
        return Risk.reversible

    def check(self, action: str, el: dict[str, Any], *, recorded_risk: Risk | None = None) -> Decision:
        if action not in self.policy.allowed_actions:
            return Decision("deny", Risk.reversible, f"action {action!r} not allowed")
        # Where would this take us? Links navigate; submit buttons post their form.
        dest = None
        if action == "click":
            if el.get("href") and not el["href"].startswith("javascript:"):
                dest = el["href"]
            elif el.get("role") == "button" and el.get("formAction"):
                dest = el["formAction"]
        if dest:
            ok, why = self.path_allowed(dest)
            if not ok:
                return Decision("deny", Risk.reversible, why)
        risk = self.classify(action, el)
        # Never trust the artifact to *lower* risk: take the max of what was
        # recorded and what the live control looks like now.
        if recorded_risk == Risk.irreversible:
            risk = Risk.irreversible
        if risk == Risk.irreversible:
            return Decision("needs_approval", risk, f"irreversible control {el.get('role')} {el.get('name')!r}")
        return Decision("allow", risk, "ok")


def route_guard(gate: Gate, on_block):
    """Playwright route handler: block document/frame navigations off the allowlist
    and any request to a foreign origin."""

    def handle(route, request):
        url = request.url
        if rel_path(url, gate.base_url) is None and not url.startswith("data:"):
            on_block(url, "foreign origin")
            return route.abort("blockedbyclient")
        if request.resource_type == "document":
            ok, why = gate.path_allowed(url)
            if not ok:
                on_block(url, why)
                return route.abort("blockedbyclient")
        return route.continue_()

    return handle
