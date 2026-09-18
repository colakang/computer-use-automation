"""Human-in-the-loop: intervention requests and control transfer of the *live* session.

Control model
-------------
Exactly one party controls the browser session at any time, tracked by
:class:`ControlChannel`::

    AUTOMATION ──request()──▶ AWAITING_HUMAN ──claim(op)──▶ HUMAN
        ▲                           │                          │
        └──────── release(resolution) ◀────────────────────────┘

* Automation calls :meth:`ControlChannel.require_automation` before *every*
  action; if it does not hold control it cannot act. (Single-threaded owner:
  the Playwright objects live on the automation thread, so the console never
  touches the page directly — it enqueues commands that the automation
  thread executes *on the human's behalf* only while ``state == HUMAN``.)
* While a human holds control, every DOM event in every frame is captured
  (``perception.js`` → ``__cuaHumanEvent``) and described with the same
  locator strategies the recorder uses, so manual steps are auditable and
  can be promoted into the artifact.
* ``release()`` carries a typed resolution (approve / deny / retry / skip /
  continue / abort) so the automation knows *how* to resume, and it always
  re-verifies state before acting again — it never assumes what the human did.

Routing: an intervention is written to ``interventions/<id>.json`` (the
stand-in for a ticket/queue/pager) and to the run's evidence, and the
operator console URL is printed. The console is a deliberately bare mock:
live masked screenshot, click-to-click, type, keys, and resolution buttons.
Operators may equally use the headed browser window itself; both paths go
through the same capture.
"""

from __future__ import annotations

import json
import sys
import queue
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .evidence import RunLog
from .surface.web import WebSurface

Resolution = Literal["approve", "deny", "retry", "skip", "continue", "abort"]


class Controller(StrEnum):
    automation = "automation"
    awaiting_human = "awaiting_human"
    human = "human"


class NotInControl(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Intervention:
    run_id: str
    kind: Literal["stuck", "approval", "replay_failure"]
    subject: str                      # goal or capability ref
    step: str                         # step id / discovery step number + intent
    reason: str
    allowed: list[Resolution]
    context: dict[str, Any] = field(default_factory=dict)
    screenshot: str | None = None
    id: str = field(default_factory=lambda: "iv-" + uuid.uuid4().hex[:8])
    created_at: str = field(default_factory=_now)
    status: Literal["open", "claimed", "resolved"] = "open"
    claimed_by: str | None = None
    resolution: Resolution | None = None
    notes: str | None = None
    resolved_at: str | None = None
    human_actions: list[dict[str, Any]] = field(default_factory=list)


class ControlChannel:
    def __init__(self, surface: WebSurface, log: RunLog, *, queue_dir: Path,
                 console_port: int = 8802, operator_timeout_s: int = 900) -> None:
        self.surface = surface
        self.log = log
        self.queue_dir = queue_dir
        self.port = console_port
        self.timeout_s = operator_timeout_s
        self.state = Controller.automation
        self.holder = "automation"
        self.current: Intervention | None = None
        self.transitions: list[dict[str, str]] = []
        self._cmds: queue.Queue[tuple] = queue.Queue()
        self._screen: bytes = b""
        self._server: uvicorn.Server | None = None

    @property
    def sensitive(self) -> list[str]:
        return self.log.redactor.literals  # live: includes values registered after start

    # ------------------------------------------------------------ state

    def _move(self, to: Controller, actor: str, why: str) -> None:
        rec = {"at": _now(), "from": self.state.value, "to": to.value, "actor": actor, "why": why}
        self.transitions.append(rec)
        self.state = to
        self.holder = actor if to != Controller.awaiting_human else "nobody"
        self.log.event("control.transition", **rec)

    def require_automation(self) -> None:
        if self.state != Controller.automation:
            raise NotInControl(f"automation does not hold control (state={self.state}, holder={self.holder})")

    # ------------------------------------------------------------ automation side

    def escalate(self, iv: Intervention) -> Intervention:
        """Pause automation, route the request, and block until a human resolves it."""
        self.require_automation()
        shot = self.surface.screenshot(self.log.next_shot(f"intervention-{iv.kind}"), self.sensitive)
        iv.screenshot = self.log.rel(shot)
        self.current = iv
        self._move(Controller.awaiting_human, "automation", f"{iv.kind}: {iv.reason}")
        self._persist(iv)
        self.log.event("intervention.raised", id=iv.id, kind=iv.kind, step=iv.step, reason=iv.reason,
                       allowed=iv.allowed, screenshot=iv.screenshot, console=self.url)
        self._ensure_console()
        print(f"\n>>> INTERVENTION {iv.id} ({iv.kind}) — {iv.reason}\n>>> operator console: {self.url}\n", file=sys.stderr, flush=True)
        self._wait(iv)
        self._persist(iv)
        self.current = None
        return iv

    def _wait(self, iv: Intervention) -> None:
        deadline = time.monotonic() + self.timeout_s
        last_shot = 0.0
        while True:
            if time.monotonic() > deadline:
                self._resolve(iv, "abort", "operator", "no operator response before timeout")
                return
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                cmd = None
            if cmd:
                if self._handle(iv, cmd):
                    return
            if time.monotonic() - last_shot > 0.7:
                try:
                    self._screen = self.surface.screenshot_bytes(self.sensitive)
                except Exception:
                    pass
                last_shot = time.monotonic()
            self.surface.pump(80)

    def _handle(self, iv: Intervention, cmd: tuple) -> bool:
        op, *args = cmd
        if op == "claim":
            if self.state != Controller.awaiting_human:
                return False
            iv.status, iv.claimed_by = "claimed", args[0]
            self.surface.human_events.clear()
            self.surface.capture_human = True
            self._move(Controller.human, args[0], "operator took control of the live session")
            self._persist(iv)
            return False
        if op in ("click", "type", "key"):
            if self.state != Controller.human:
                return False  # console input is ignored unless the human holds control
            if op == "click":
                self.surface.human_click(*args)
                self.log.event("human.console_input", op="click", x=args[0], y=args[1])
            elif op == "type":
                self.surface.human_type(args[0])
                self.log.event("human.console_input", op="type", chars=len(args[0]))
            else:
                self.surface.human_key(args[0])
                self.log.event("human.console_input", op="key", key=args[0])
            return False
        if op == "release":
            resolution, actor, notes = args
            if resolution not in iv.allowed:
                self.log.event("intervention.rejected_resolution", resolution=resolution, allowed=iv.allowed)
                return False
            self._resolve(iv, resolution, actor, notes)
            return True
        return False

    def _resolve(self, iv: Intervention, resolution: Resolution, actor: str, notes: str | None) -> None:
        self.surface.pump(300)  # flush in-flight human events
        self.surface.capture_human = False
        iv.human_actions = list(self.surface.human_events)
        self.surface.human_events.clear()
        iv.status, iv.resolution, iv.notes, iv.resolved_at = "resolved", resolution, notes, _now()
        self.log.event("intervention.resolved", id=iv.id, resolution=resolution, by=actor, notes=notes,
                       human_actions=[_summarize(a) for a in iv.human_actions])
        self._move(Controller.automation, "automation", f"resumed after {resolution} by {actor}")

    def _persist(self, iv: Intervention) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        data = self.log.redactor.obj(asdict(iv) | {"control": {"state": self.state.value, "holder": self.holder}})
        (self.queue_dir / f"{iv.id}.json").write_text(json.dumps(data, indent=2))
        self.log.write_json(f"intervention-{iv.id}.json", data, redact=False)

    # ------------------------------------------------------------ operator console (mock)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def _ensure_console(self) -> None:
        if self._server:
            return
        app = _console_app(self)
        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(50):
            if self._server.started:
                break
            time.sleep(0.05)

    def shutdown(self) -> None:
        if self._server:
            self._server.should_exit = True

    def public_state(self) -> dict[str, Any]:
        iv = self.current
        return {
            "control": {"state": self.state.value, "holder": self.holder},
            "intervention": None if iv is None else self.log.redactor.obj(
                {k: v for k, v in asdict(iv).items() if k != "human_actions"}
            ),
            "transitions": self.transitions[-8:],
        }


def _summarize(ev: dict[str, Any]) -> dict[str, Any]:
    t = ev.get("target") or {}
    return {
        "type": ev.get("type"),
        "frame": ev.get("frame"),
        "role": t.get("role"),
        "name": t.get("name") or t.get("label"),
        "option": ev.get("option"),
        "chars": ev.get("length"),
    }


def _console_app(ch: ControlChannel) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.get("/api/state")
    def state() -> JSONResponse:
        return JSONResponse(ch.public_state())

    @app.get("/api/screen.jpg")
    def screen() -> Response:
        return Response(ch._screen, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/{op}")
    async def command(op: str, request: Request) -> JSONResponse:
        body = await request.json() if (await request.body()) else {}
        operator = body.get("operator", "operator")
        if op == "claim":
            ch._cmds.put(("claim", operator))
        elif op == "click":
            ch._cmds.put(("click", float(body["x"]), float(body["y"])))
        elif op == "type":
            ch._cmds.put(("type", str(body["text"])))
        elif op == "key":
            ch._cmds.put(("key", str(body["key"])))
        elif op == "release":
            ch._cmds.put(("release", body["resolution"], operator, body.get("notes")))
        else:
            return JSONResponse({"error": "unknown op"}, status_code=404)
        return JSONResponse({"queued": op})

    return app


CONSOLE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>CUA operator console</title>
<style>
body{font:14px system-ui,sans-serif;margin:0;background:#f5f5f2;color:#222}
header{background:#1d2733;color:#fff;padding:10px 16px;display:flex;gap:16px;align-items:center}
.pill{padding:2px 10px;border-radius:12px;font-weight:600}
.automation{background:#2e7d32}.awaiting_human{background:#e65100}.human{background:#1565c0}
main{display:grid;grid-template-columns:1fr 360px;gap:16px;padding:16px}
#screen{width:100%;border:1px solid #999;cursor:crosshair;background:#333}
.card{background:#fff;border:1px solid #ddd;border-radius:6px;padding:12px;margin-bottom:12px}
button{margin:3px 3px 3px 0;padding:6px 10px}
pre{white-space:pre-wrap;font-size:12px;max-height:220px;overflow:auto;background:#fafafa;padding:6px}
</style></head><body>
<header><b>Operator console</b><span>control:</span><span id="ctl" class="pill">-</span><span id="holder"></span></header>
<main><div><img id="screen" alt="live session (sensitive fields masked)"><div style="font-size:12px;color:#666">Live session, sensitive values masked. Click the image to click in the session (only while you hold control).</div></div>
<div><div class="card" id="iv">No open intervention.</div>
<div class="card"><b>Control</b><br><input id="op" value="operator@cu" size="16"> <button onclick="post('claim')">Take control</button><br>
<input id="txt" placeholder="text to type" size="22"> <button onclick="post('type',{text:txt.value});txt.value=''">Type</button><br>
<button onclick="post('key',{key:'Enter'})">Enter</button><button onclick="post('key',{key:'Tab'})">Tab</button><button onclick="post('key',{key:'Escape'})">Esc</button></div>
<div class="card"><b>Hand back</b><div id="res"></div><input id="notes" placeholder="notes (optional)" size="30"></div>
<div class="card"><b>Transitions</b><pre id="tr"></pre></div></div></main>
<script>
// Everything shown here originates from the automated page: always escape.
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={approve:'Approve & resume',deny:'Deny',retry:'Resume: retry step',skip:'Resume: I completed this step',continue:'Resume agent',abort:'Abort run'};
async function post(op,body={}){body.operator=document.getElementById('op').value;await fetch('/api/'+op,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});}
const img=document.getElementById('screen');
img.onclick=e=>{const r=img.getBoundingClientRect();post('click',{x:(e.clientX-r.left)*img.naturalWidth/r.width,y:(e.clientY-r.top)*img.naturalHeight/r.height});};
async function tick(){try{const s=await (await fetch('/api/state')).json();
ctl.textContent=s.control.state;ctl.className='pill '+s.control.state;holder.textContent='holder: '+s.control.holder;
const iv=s.intervention;
if(iv){document.getElementById('iv').innerHTML='<b>'+esc(iv.id)+' · '+esc(iv.kind)+'</b><br><b>Subject:</b> '+esc(iv.subject)+'<br><b>Step:</b> '+esc(iv.step)+'<br><b>Why:</b> '+esc(iv.reason)+'<pre>'+esc(JSON.stringify(iv.context,null,1))+'</pre>';
res.innerHTML=iv.allowed.map(a=>'<button onclick="post(\\'release\\',{resolution:\\''+a+'\\',notes:notes.value})">'+labels[a]+'</button>').join('');}
else{document.getElementById('iv').textContent='No open intervention.';res.innerHTML='';}
tr.textContent=s.transitions.map(t=>t.at.slice(11)+' '+t.from+' → '+t.to+' ('+t.actor+')').join('\\n');
img.src='/api/screen.jpg?t='+Date.now();}catch(e){}}
setInterval(tick,1000);tick();
</script></body></html>"""
