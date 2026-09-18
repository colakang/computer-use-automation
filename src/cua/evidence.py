"""Per-run evidence: structured JSONL log, masked screenshots, redacted failure snapshots.

Every record goes through the redactor on the way to disk — there is no
"raw" logging path. Playwright traces are deliberately *not* used: they
capture typed values and full DOM (credentials, PII) and cannot be redacted
after the fact.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redact import Redactor


class RunLog:
    def __init__(self, root: Path, kind: str, slug: str, redactor: Redactor) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.run_id = f"{ts}-{kind}-{slug}"
        self.dir = root / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "screens").mkdir(exist_ok=True)
        self.redactor = redactor
        self._f = (self.dir / "log.jsonl").open("a", encoding="utf-8")
        self._t0 = time.monotonic()
        self._shot = 0

    def event(self, event: str, **fields: Any) -> None:
        rec = {
            "t": round(time.monotonic() - self._t0, 3),
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
            **self.redactor.obj(fields),
        }
        self._f.write(json.dumps(rec, default=str) + "\n")
        self._f.flush()

    def next_shot(self, label: str) -> Path:
        self._shot += 1
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        return self.dir / "screens" / f"{self._shot:03d}-{safe}.png"

    def write_text(self, name: str, text: str) -> Path:
        p = self.dir / name
        p.write_text(self.redactor.text(text), encoding="utf-8")
        return p

    def write_json(self, name: str, obj: Any, *, redact: bool = True) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.obj(obj) if redact else obj, indent=2, default=str), encoding="utf-8")
        return p

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.dir.parent.parent))
        except ValueError:
            return str(p)

    def close(self) -> None:
        self._f.close()
