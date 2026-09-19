"""Pixel-only implementation of the Surface seam (prototype).

What this surface is allowed to use — the same things an RDP/Citrix session offers:

* **perceive**: a screenshot. No DOM, no accessibility tree, no URLs.
  (``perception.js`` is *not* injected; nothing here evaluates JS in the page.)
* **act**: mouse clicks at coordinates and keystrokes.

A pinned screen parser (a VLM) turns each screenshot into elements with role,
text, visual caption, table position and bounding box. The *same* locator
strategies as the DOM surface resolve against those elements —
``role_name``, ``label``, ``table_cell``; ``attr``/``css`` are unavailable on
pixels and simply never match. The bounding box is only used to deliver the
click; it never identifies the target.

Determinism boundary: replay uses a model here to *perceive* (like the web
surface uses a browser engine to render), never to *decide*. The parser is
pinned (model + prompt hash, logged on every run), each parse is cached by a
perceptual fingerprint of the screen, and every resolution still has to be
unique and every action is still verified by the next checkpoint.

The browser underneath is only the stand-in for "a remote desktop showing an
app": it is used for screenshots, mouse and keyboard, plus native-dialog
handling and the network allowlist that a real VDI would provide elsewhere.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

from ..redact import PATTERNS
from ..schema import Strategy, Target
from .base import Observation, Resolution
from .web import ActionError, WebSurface

PARSER_PROMPT = """You are the screen parser of a GUI automation system. You receive one screenshot
of a desktop application. Describe what is on screen as ONE JSON object, nothing else:

{"elements": [{"role": "button|link|textbox|combobox|checkbox|radio|heading|text",
               "text": "<visible label/text exactly as shown>",
               "caption": "<for textbox/combobox: the visible label beside or above it, exactly as shown, else empty>",
               "value": "<for textbox/combobox: current visible content, else empty>",
               "bbox": [x0, y0, x1, y1],
               "obscured": <true if covered by a modal/overlay so it cannot be clicked>}],
 "tables": [{"header": ["<header cell text>", ...] or null,
             "rows": [[{"text": "<cell text>", "bbox": [x0, y0, x1, y1]}, ...], ...]}]}

Rules:
- Coordinates are pixels of this image (width {W}, height {H}), origin top-left.
- Put data grids and label/value tables in "tables" (header = null when there is no header row,
  e.g. "Name: | Dana"). Do NOT put form layouts (caption + input) in tables.
- Links or buttons inside table cells: list them in "elements" as well.
- Transcribe text exactly. Never invent text that is not visible. Areas painted solid dark are redacted: skip them.
- Include every clickable or editable control, including ones inside dialogs/popups.
- If a dialog/popup sits over a dimmed or darkened page, EVERY control outside that dialog is obscured: mark it "obscured": true.
- Also include headings, notices, messages and error text (role heading/text) — status text matters.
"""
PROMPT_HASH = hashlib.sha256(PARSER_PROMPT.encode()).hexdigest()[:12]


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().rstrip(":").strip().casefold()


# --------------------------------------------------------------------------- parser


class VLMScreenParser:
    """Screenshot → structured elements, via Claude (``claude -p`` with an image content block)."""

    def __init__(self, model: str = "sonnet") -> None:
        self.model = model
        self.pin = f"claude-cli:{model}#prompt-{PROMPT_HASH}"

    def parse(self, png: bytes, size: tuple[int, int]) -> dict[str, Any]:
        msg = {"type": "user", "message": {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": PARSER_PROMPT.replace("{W}", str(size[0])).replace("{H}", str(size[1]))},
        ]}}
        cmd = ["claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--model", self.model, "--tools", "", "--no-session-persistence", "--setting-sources", "",
               "--strict-mcp-config"]
        p = subprocess.run(cmd, input=json.dumps(msg) + "\n", capture_output=True, text=True, timeout=180)
        final = next((json.loads(l) for l in reversed(p.stdout.splitlines()) if '"type":"result"' in l.replace(" ", "")), None)
        if p.returncode != 0 or not final or final.get("is_error"):
            raise RuntimeError(f"screen parser failed: {(final or {}).get('result') or p.stderr[-300:]}")
        m = re.search(r"\{.*\}", final["result"], re.S)
        if not m:
            raise RuntimeError("screen parser returned no JSON")
        return json.loads(m.group(0))


# --------------------------------------------------------------------------- parsed screen


@dataclass
class VElement:
    ref: str
    role: str
    name: str                       # visible text (buttons/links/cells)
    label: str                      # caption beside a field
    value: str
    bbox: tuple[float, float, float, float]
    obscured: bool = False
    table: int | None = None        # table index for cells
    column: str | None = None       # header text or "#i"
    row: dict[str, str] = field(default_factory=dict)  # column -> cell text, for the cell's row
    header: bool = False

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return (x0 + x1) / 2, (y0 + y1) / 2


def build_elements(parsed: dict[str, Any]) -> list[VElement]:
    out: list[VElement] = []
    n = 0
    for e in parsed.get("elements", []):
        if not isinstance(e.get("bbox"), list) or len(e["bbox"]) != 4:
            continue
        n += 1
        role = e.get("role", "text")
        text = e.get("text") or ""
        name = "" if role in ("textbox", "combobox") else text
        label = e.get("caption") or (text if role in ("textbox", "combobox") else "")
        out.append(VElement(f"v:e{n}", role, name, label, e.get("value") or "", tuple(e["bbox"]), bool(e.get("obscured"))))
    for ti, t in enumerate(parsed.get("tables", [])):
        header = t.get("header")
        rows = t.get("rows") or []
        colname = (lambda i: header[i] if header and i < len(header) else f"#{i}")
        for r in rows:
            texts = {colname(i): (c.get("text") or "") for i, c in enumerate(r)}
            for ci, c in enumerate(r):
                if not isinstance(c.get("bbox"), list) or len(c["bbox"]) != 4:
                    continue
                n += 1
                out.append(VElement(f"v:e{n}", "cell", c.get("text") or "", "", "", tuple(c["bbox"]),
                                    table=ti, column=colname(ci), row=texts))
    return out


def matches(el: VElement, s: Strategy) -> bool:
    by = s.by
    if by == "role_name":
        return el.role == s.role and _norm(el.name) == _norm(s.name)
    if by == "label":
        return el.role == s.role and _norm(el.label) == _norm(s.label)
    if by == "table_cell":
        if el.role != "cell" or _norm(el.column) != _norm(s.column):
            return False
        key = next((v for k, v in el.row.items() if _norm(k) == _norm(s.row.column)), None)
        return key is not None and _norm(key) == _norm(s.row.equals)
    return False  # attr / css: not observable on pixels


# --------------------------------------------------------------------------- surface


class VisionSurface(WebSurface):
    sees_urls = False
    time_scale = 3.0          # a parse takes seconds, not milliseconds

    def __init__(self, base_url: str, gate, *, parser: VLMScreenParser | None = None,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None, **kw: Any) -> None:
        super().__init__(base_url, gate, on_event=on_event, inject_perception=False, **kw)
        self.parser = parser or VLMScreenParser()
        self._cache: list[tuple[str, list[VElement], dict[str, Any]]] = []
        self._current: list[VElement] = []
        self.stats = {"parses": 0, "cache_hits": 0, "parse_seconds": 0.0}
        self.on_event("vision.parser", {"pin": self.parser.pin})

    # ------------------------------------------------------------ pixels → elements

    def _grab(self) -> tuple[bytes, Image.Image]:
        for attempt in range(3):
            try:
                png = self.page.screenshot(type="png", timeout=15_000)
                break
            except Exception:
                if attempt == 2:
                    raise
                self.page.wait_for_timeout(500)
        img = Image.open(io.BytesIO(png)).convert("RGB")
        return png, img

    @staticmethod
    def _sig(img: Image.Image) -> Image.Image:
        return img.convert("L").resize((img.width // 10, img.height // 10))

    @staticmethod
    def _diff(a: Image.Image, b: Image.Image) -> float:
        if a.size != b.size:
            return 255.0
        pa, pb = a.tobytes(), b.tobytes()
        return sum(abs(x - y) for x, y in zip(pa, pb)) / len(pa)

    def _stable(self, max_wait_s: float = 4.0) -> tuple[bytes, Image.Image]:
        """Wait until two consecutive frames look the same (page done painting)."""
        png, img = self._grab()
        end = time.monotonic() + max_wait_s
        while time.monotonic() < end:
            self.page.wait_for_timeout(250)
            png2, img2 = self._grab()
            if self._diff(self._sig(img), self._sig(img2)) < 0.5:
                return png2, img2
            png, img = png2, img2
        return png, img

    @staticmethod
    def _key(img: Image.Image) -> str:
        """Cache key. Deliberately *exact* (half resolution, 32 grey levels): a
        fuzzy match would let a screen with a different balance in the same
        layout reuse the old parse — a stale read. A blinking caret only costs
        an extra parse; a stale value costs a wrong answer."""
        small = img.convert("L").resize((img.width // 2, img.height // 2)).point(lambda v: v // 8)
        return hashlib.sha1(small.tobytes()).hexdigest()

    def screen_token(self) -> str:
        """Exact fingerprint of what is on screen now (used as a visual post-condition)."""
        _, img = self._stable()
        return self._key(img)

    def elements(self) -> list[VElement]:
        png, img = self._stable()
        key = self._key(img)
        for ckey, els, _ in self._cache:
            if ckey == key:
                self.stats["cache_hits"] += 1
                self._current = els
                return els
        t0 = time.monotonic()
        parsed = self.parser.parse(png, img.size)
        dt = time.monotonic() - t0
        els = build_elements(parsed)
        self.stats["parses"] += 1
        self.stats["parse_seconds"] += dt
        self.on_event("vision.parse", {"seconds": round(dt, 1), "elements": len(els), "pin": self.parser.pin})
        self._cache.append((key, els, parsed))
        self._cache = self._cache[-32:]
        self._current = els
        return els

    def _by_ref(self, ref: str) -> VElement:
        el = next((e for e in self._current if e.ref == ref), None)
        if el is None:
            raise ActionError(f"ref {ref} not on the current screen parse")
        return el

    # ------------------------------------------------------------ Surface protocol: perception

    def observe(self, opaque: bool = False) -> Observation:
        els = self.elements()
        lines = []
        for e in els:
            if e.role == "cell":
                continue
            line = f"[{e.ref}] {e.role}"
            if e.name:
                line += f' "{e.name}"'
            if e.label and not e.name:
                line += f' label="{e.label}"'
            if e.role in ("textbox", "combobox"):
                line += f' value="{("‹" + str(len(e.value)) + "›") if opaque and e.value else e.value}"'
            if e.obscured:
                line += " (obscured)"
            lines.append(line)
        tables: dict[int, list[VElement]] = {}
        for e in els:
            if e.role == "cell":
                tables.setdefault(e.table or 0, []).append(e)
        for ti, cells in tables.items():
            lines.append("table:")
            cols = list(dict.fromkeys(c.column for c in cells))
            if not cols[0].startswith("#"):
                lines.append("  header: " + " | ".join(cols))
            rows: dict[tuple, list[VElement]] = {}
            for c in cells:
                rows.setdefault(tuple(sorted(c.row.items())), []).append(c)
            for rc in rows.values():
                shown = [(f"‹{len(c.name)}›" if opaque and not (c.column or "").startswith("#0") else c.name) for c in rc]
                lines.append("  row: " + " | ".join(f'[{c.ref}] "{t}"' for c, t in zip(rc, shown)))
        text = "=== screen (pixels only) ===\n" + "\n".join(lines)
        return Observation(text=text, locations={}, digest=hashlib.sha1(text.encode()).hexdigest()[:12])

    def frame_texts(self) -> dict[str, str]:
        # No frames on a pixel surface: one "*" pseudo-frame (Match.hit ignores frame filters for it).
        return {"*": " ".join(filter(None, (e.name or e.label or e.value for e in self.elements())))}

    def locations(self) -> dict[str, str]:
        return {}

    def resolve_strategy(self, frame: str, strategy: Strategy) -> list[str]:
        return [e.ref for e in self._current if matches(e, strategy)]

    def resolve(self, target: Target) -> Resolution:
        self.elements()
        counts: list[int] = []
        for i, s in enumerate(target.strategies):
            refs = self.resolve_strategy(target.frame, s)
            counts.append(len(refs))
            if len(refs) == 1:
                return Resolution(refs[0], i, counts)
        return Resolution(None, None, counts)

    def element_info(self, ref: str) -> dict[str, Any]:
        e = self._by_ref(ref)
        strategies: list[dict[str, Any]] = []
        if e.name and e.role not in ("cell", "text", "heading"):
            strategies.append({"by": "role_name", "role": e.role, "name": e.name})
        if e.label and not e.name:
            strategies.append({"by": "label", "role": e.role, "label": e.label})
        if e.role == "cell":
            keys = [(k, v) for k, v in e.row.items() if k != e.column and v and not re.search(r"\d", v)]
            if keys:
                k, v = keys[0]
                strategies.append({"by": "table_cell", "column": e.column, "row": {"column": k, "equals": v}})
        from ..schema import Target as T

        for s in strategies:
            s["unique"] = len(self.resolve_strategy("*", T.model_validate(
                {"frame": "*", "description": "", "strategies": [s]}).strategies[0])) == 1
        # Destination (href / form target) is invisible on pixels: the gate can only
        # judge by role + name here; the network allowlist remains the backstop.
        return {"role": e.role, "name": e.name, "label": e.label, "tag": None, "href": None,
                "formAction": None, "formMethod": None, "strategies": strategies, "frame": "*",
                "bbox": e.bbox, "obscured": e.obscured}

    def read(self, ref: str) -> str | None:
        e = self._by_ref(ref)
        return e.name or e.value

    # ------------------------------------------------------------ actions: mouse + keyboard only

    def _point(self, ref: str) -> tuple[float, float]:
        e = self._by_ref(ref)
        if e.obscured:
            # On pixels a click on a covered control silently lands on the overlay.
            # Refuse instead of mis-clicking.
            raise ActionError(f"{e.role} {e.name or e.label!r} is obscured by an overlay")
        return e.center

    def click(self, ref: str) -> None:
        x, y = self._point(ref)
        self.page.mouse.click(x, y)

    def fill(self, ref: str, value: str) -> None:
        x, y = self._point(ref)
        self.page.mouse.click(x, y)
        self.page.keyboard.press("ControlOrMeta+A")
        self.page.keyboard.type(value)

    def select(self, ref: str, option: str) -> None:
        x, y = self._point(ref)
        self.page.mouse.click(x, y)
        self.page.keyboard.type(option)
        self.page.keyboard.press("Enter")

    def press(self, ref: str, key: str) -> None:
        x, y = self._point(ref)
        self.page.mouse.click(x, y)
        self.page.keyboard.press(key)

    def reload_frame(self, name: str) -> None:
        raise ActionError("pixel surface cannot reload a single frame")

    def settle(self, ms: int = 350) -> None:
        self.page.wait_for_timeout(ms)

    def human_click(self, x: float, y: float) -> None:
        # Hit-test the last parse so the operator's click is recorded as a control, not a coordinate.
        hit = next((e for e in self._current if e.bbox[0] <= x <= e.bbox[2] and e.bbox[1] <= y <= e.bbox[3]), None)
        if hit and self.capture_human:
            self.human_events.append({"type": "click", "frame": "*", "target": self.element_info(hit.ref)})
        self.page.mouse.click(x, y)

    # ------------------------------------------------------------ evidence: mask by parsed geometry

    def _masked(self, sensitive: list[str]) -> Image.Image:
        els = self.elements()
        _, img = self._grab()
        draw = ImageDraw.Draw(img)
        pats = [re.compile(p) for p in PATTERNS.values()]
        for e in els:
            t = e.name or e.value
            data_cell = e.role == "cell" and not (e.column or "").startswith("#0")
            if data_cell or (t and (any(p.search(t) for p in pats) or any(s and s in t for s in sensitive))):
                draw.rectangle(e.bbox, fill=(48, 48, 48))
        return img

    def screenshot(self, path: Path, sensitive: list[str]) -> Path:
        self._masked(sensitive).save(path)
        return path

    def screenshot_bytes(self, sensitive: list[str]) -> bytes:
        buf = io.BytesIO()
        self._masked(sensitive).save(buf, format="JPEG", quality=70)
        return buf.getvalue()
