"""Web implementation of the Surface seam (Playwright, sync API, one live session).

Perception is our own JS (``perception.js``), not Playwright's selector
engines, so recording and replay share one definition of role/name/label.
The browser session is owned by this object for its whole life — discovery,
replay, *and* human takeover all operate on the same page; that is what
makes the handoff real.
"""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Frame, Page, sync_playwright

from ..config import DialogRule
from ..policy import Gate, rel_path, route_guard
from ..redact import JS_PATTERNS
from ..schema import Strategy, Target
from .base import Observation, Resolution

PERCEPTION_JS = (Path(__file__).parent / "perception.js").read_text()
ACTION_TIMEOUT_MS = 4_000


class ActionError(Exception):
    """An action could not be performed on a resolved element (obstructed, detached...)."""


class WebSurface:
    def __init__(
        self,
        base_url: str,
        gate: Gate,
        *,
        headed: bool = False,
        dialog_rules: list[DialogRule] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        cdp_port: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.gate = gate
        self.dialog_rules = dialog_rules or []
        self.on_event = on_event or (lambda e, f: None)
        self.human_events: list[dict[str, Any]] = []
        self.capture_human = False
        self.unexpected_dialogs: list[str] = []
        self.known_dialogs: list[dict[str, str]] = []
        self.blocked_requests: list[dict[str, str]] = []
        self._lock = threading.Lock()

        self._pw = sync_playwright().start()
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        self.browser = self._pw.chromium.launch(headless=not headed, args=args)
        self.context = self.browser.new_context(viewport={"width": 1180, "height": 760})
        self.context.add_init_script(PERCEPTION_JS)
        self.context.expose_binding("__cuaHumanEvent", self._on_human_event)
        self.context.route("**/*", route_guard(gate, self._on_block))
        self.page: Page = self.context.new_page()
        self.page.on("dialog", self._on_dialog)
        self.page.set_default_timeout(ACTION_TIMEOUT_MS)

    # ---------------------------------------------------------------- events

    def _on_block(self, url: str, why: str) -> None:
        self.blocked_requests.append({"url": url, "why": why})
        self.on_event("policy.network_block", {"url": url, "why": why})

    def _on_dialog(self, dialog) -> None:
        msg = dialog.message
        for rule in self.dialog_rules:
            if re.search(rule.match, msg, re.I):
                self.on_event("dialog.known", {"message": msg, "action": rule.action, "rule": rule.description})
                self.known_dialogs.append({"message": msg, "action": rule.action})
                dialog.accept() if rule.action == "accept" else dialog.dismiss()
                return
        # Unknown dialog: dismiss (the conservative choice — "Cancel") and
        # let the caller decide; replay treats this as a hard failure.
        self.unexpected_dialogs.append(msg)
        self.on_event("dialog.unexpected", {"message": msg, "action": "dismiss"})
        dialog.dismiss()

    def _on_human_event(self, source, payload: dict[str, Any]) -> None:
        if self.capture_human:
            with self._lock:
                self.human_events.append(payload)

    # ---------------------------------------------------------------- frames

    def _frames(self) -> dict[str, Frame]:
        out: dict[str, Frame] = {}
        for f in self.page.frames:
            if f.is_detached():
                continue
            name = "_top" if f == self.page.main_frame else (f.name or f"frame{len(out)}")
            out[name] = f
        return out

    def _frame(self, name: str) -> Frame:
        f = self._frames().get(name)
        if f is None:
            raise ActionError(f"frame {name!r} not present")
        return f

    def _eval(self, frame: Frame, expr: str, arg: Any = None) -> Any:
        try:
            return frame.evaluate(f"(a) => window.__cua ? {expr} : null", arg)
        except Exception:
            return None  # frame navigating; callers poll

    # ---------------------------------------------------------------- perception

    def observe(self, opaque: bool = False) -> Observation:
        """opaque=True: evidence variant with data cells reduced to their shape."""
        parts: list[str] = []
        for name, f in self._frames().items():
            snap = self._eval(f, "window.__cua.snapshot(a.p, a.o)", {"p": f"{name}:", "o": opaque})
            if snap:
                path = rel_path(f.url, self.base_url) or f.url
                parts.append(f"=== frame {name} ({path}) ===\n{snap}")
        text = "\n".join(parts)
        return Observation(text=text, locations=self.locations(), digest=hashlib.sha1(text.encode()).hexdigest()[:12])

    def frame_texts(self) -> dict[str, str]:
        out = {}
        for name, f in self._frames().items():
            t = self._eval(f, "window.__cua.pageText()")
            if t:
                out[name] = t
        return out

    def locations(self) -> dict[str, str]:
        return {n: (rel_path(f.url, self.base_url) or f.url) for n, f in self._frames().items()}

    def element_info(self, ref: str) -> dict[str, Any]:
        frame = ref.split(":", 1)[0]
        info = self._eval(self._frame(frame), "window.__cua.describe(a)", ref)
        if not info:
            raise ActionError(f"ref {ref} not found")
        info["frame"] = frame
        return info

    def resolve_strategy(self, frame: str, strategy: Strategy) -> list[str]:
        f = self._frames().get(frame)
        if f is None:
            return []
        return self._eval(f, "window.__cua.resolve(a.s, a.p)", {"s": strategy.model_dump(), "p": f"{frame}:"}) or []

    def resolve(self, target: Target) -> Resolution:
        counts: list[int] = []
        for i, s in enumerate(target.strategies):
            refs = self.resolve_strategy(target.frame, s)
            counts.append(len(refs))
            if len(refs) == 1:
                return Resolution(refs[0], i, counts)
        return Resolution(None, None, counts)

    def read(self, ref: str) -> str | None:
        return self._eval(self._frame(ref.split(":", 1)[0]), "window.__cua.readText(a)", ref)

    # ---------------------------------------------------------------- actions

    def _loc(self, ref: str):
        frame, _ = ref.split(":", 1)
        return self._frame(frame).locator(f'[data-cua-ref="{ref}"]')

    def _do(self, what: str, fn) -> None:
        try:
            fn()
        except Exception as e:  # Playwright TimeoutError / Error
            msg = str(e).split("\n")
            detail = next((m.strip() for m in msg if "intercepts pointer" in m), msg[0])
            raise ActionError(f"{what}: {detail}") from None

    def click(self, ref: str) -> None:
        self._do(f"click {ref}", lambda: self._loc(ref).click(timeout=ACTION_TIMEOUT_MS))

    def fill(self, ref: str, value: str) -> None:
        self._do(f"fill {ref}", lambda: self._loc(ref).fill(value, timeout=ACTION_TIMEOUT_MS))

    def select(self, ref: str, option: str) -> None:
        self._do(f"select {ref}", lambda: self._loc(ref).select_option(label=option, timeout=ACTION_TIMEOUT_MS))

    def press(self, ref: str, key: str) -> None:
        self._do(f"press {ref}", lambda: self._loc(ref).press(key, timeout=ACTION_TIMEOUT_MS))

    def goto(self, rel: str) -> None:
        self.page.goto(self.base_url + rel, wait_until="load")

    def reload_frame(self, name: str) -> None:
        f = self._frame(name)
        f.goto(f.url, wait_until="load")

    def settle(self, ms: int = 350) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=10_000)
        except Exception:
            pass
        self.page.wait_for_timeout(ms)

    def pump(self, ms: int = 100) -> None:
        """Let Playwright deliver events (bindings, dialogs) while we wait."""
        self.page.wait_for_timeout(ms)

    # ---------------------------------------------------------------- human bridge (operator console)

    def human_click(self, x: float, y: float) -> None:
        self.page.mouse.click(x, y)

    def human_type(self, text: str) -> None:
        self.page.keyboard.type(text)

    def human_key(self, key: str) -> None:
        self.page.keyboard.press(key)

    # ---------------------------------------------------------------- evidence

    def _masks(self, sensitive: list[str]) -> list:
        """Mark sensitive leaves in every frame; return locators for Playwright to paint over.
        Masking happens *before* pixels are captured, so unmasked images never exist on disk."""
        masks = []
        for f in self._frames().values():
            self._eval(f, "window.__cua.markSensitive(a.p, a.l)", {"p": JS_PATTERNS, "l": sensitive})
            masks.append(f.locator("[data-cua-sensitive]"))
        return masks

    def screenshot(self, path: Path, sensitive: list[str]) -> Path:
        self.page.screenshot(path=str(path), mask=self._masks(sensitive), mask_color="#303030")
        return path

    def screenshot_bytes(self, sensitive: list[str]) -> bytes:
        return self.page.screenshot(mask=self._masks(sensitive), mask_color="#303030", type="jpeg", quality=70)

    def close(self) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()
