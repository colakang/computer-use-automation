"""The seam between "how we perceive/act on a surface" and "the recorded flow".

Everything above this interface — agent loop, recorder, replay engine,
policy, escalation — speaks only in terms of:

* an **observation** (text rendering with opaque refs, per frame/window),
* **element info** (role, name, caption, where activating it leads),
* **Target** / strategies from ``cua.schema`` (resolved to exactly one ref),
* primitive **actions** on a ref.

A web surface implements this with Playwright + injected JS (``web.py``).
A desktop surface would implement it with UI Automation (Windows) or AX
(macOS): frames become window paths, ``role_name`` maps to ControlType+Name,
``label`` to the preceding static text in the tab order, ``attr`` to
AutomationId, ``table_cell`` to Grid/Table patterns. A terminal/3270 surface
would map to field positions on a screen buffer. The artifact schema and the
replay engine do not change — only strategy kinds a given surface supports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..schema import Strategy, Target


@dataclass
class Observation:
    text: str                          # what the model sees
    locations: dict[str, str]          # frame/window -> url/path
    digest: str                        # stable hash for stuck detection / logs


@dataclass
class Resolution:
    ref: str | None
    strategy_index: int | None         # which strategy matched uniquely
    counts: list[int] = field(default_factory=list)  # match count per strategy (debug evidence)

    @property
    def drifted(self) -> bool:
        return self.ref is not None and (self.strategy_index or 0) > 0

    @property
    def ambiguous(self) -> bool:
        return self.ref is None and any(c > 1 for c in self.counts)


class Surface(Protocol):
    def observe(self) -> Observation: ...
    def frame_texts(self) -> dict[str, str]: ...
    def locations(self) -> dict[str, str]: ...
    def element_info(self, ref: str) -> dict[str, Any]: ...
    def resolve(self, target: Target) -> Resolution: ...
    def resolve_strategy(self, frame: str, strategy: Strategy) -> list[str]: ...
    def click(self, ref: str) -> None: ...
    def fill(self, ref: str, value: str) -> None: ...
    def select(self, ref: str, option: str) -> None: ...
    def press(self, ref: str, key: str) -> None: ...
    def read(self, ref: str) -> str | None: ...
    def screenshot(self, path: Path, sensitive: list[str]) -> Path: ...
    def goto(self, rel: str) -> None: ...
