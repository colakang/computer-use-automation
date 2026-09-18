"""Redaction for everything that is persisted (logs, snapshots, artifacts, screenshots).

Three layers, cheapest-first:

1. **Known values** — secrets read from the store and any input/output value
   tagged above ``public`` are registered at runtime and replaced exactly.
   This is the strong guarantee: a value we *know* is sensitive cannot leak.
2. **Patterns** — SSN, card/account-like digit runs, money, email, phone.
   Catches sensitive data we never declared (e.g. a balance on a page the
   capability didn't ask about).
3. **Structure** — observations are logged as digests, not text; full text is
   only kept (redacted) for failure evidence. Screenshots are masked in the
   browser before capture.

Known limit: free-text PII we never declared and that has no pattern (a
member's name on a page we just pass through) is only caught if it is a
declared output/input. See REPORT.md §Safety.
"""

from __future__ import annotations

import re
from typing import Any

from .schema import Sensitivity

PATTERNS: dict[str, str] = {
    "SSN": r"\b\d{3}-\d{2}-\d{4}\b",
    "SSN4": r"\*{3}-\*{2}-\d{4}",
    "DOB": r"(?:\*\*|\d{2})/(?:\*\*|\d{2})/\d{4}",
    "CARD": r"\b(?:\d[ -]?){13,19}\b",
    "MONEY": r"\$\s?-?[\d,]+(?:\.\d{2})?",
    "EMAIL": r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b",
    "PHONE": r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b",
}

# JS-compatible versions for in-browser screenshot masking.
JS_PATTERNS = [PATTERNS["SSN"], PATTERNS["SSN4"], PATTERNS["DOB"], PATTERNS["MONEY"], PATTERNS["EMAIL"]]


def mask_value(v: str) -> str:
    """Keep just enough to correlate in logs (last 2 chars) — never the value."""
    return "…" + v[-2:] if len(v) > 4 else "…"


class Redactor:
    def __init__(self) -> None:
        self._exact: dict[str, str] = {}

    def register_secret(self, value: str) -> None:
        if value:
            self._exact[value] = "[SECRET]"

    def register(self, name: str, value: str, sensitivity: Sensitivity) -> None:
        if not value or sensitivity == Sensitivity.public:
            return
        if sensitivity == Sensitivity.secret:
            self.register_secret(value)
        elif sensitivity == Sensitivity.internal:
            self._exact[value] = f"[{name}:{mask_value(value)}]"
        else:
            self._exact[value] = f"[{sensitivity.value.upper()}:{name}]"

    @property
    def literals(self) -> list[str]:
        return list(self._exact)

    def text(self, s: str) -> str:
        # Longest first so a value containing another is replaced whole.
        for v in sorted(self._exact, key=len, reverse=True):
            s = s.replace(v, self._exact[v])
        for label, pat in PATTERNS.items():
            s = re.sub(pat, f"[{label}]", s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o
