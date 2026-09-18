"""CoreOne Teller — a deliberately legacy, hostile back-office app used as the proxy target.

It imitates the kind of vendor core-banking UI a credit union actually runs:

* server-rendered HTML inside a <frameset> (banner / nav / main frames),
* table-based layout, <font> tags, no <label>s, no ids, no test ids,
  opaque form-field names (F_0107, F_0108, ...),
* a session cookie that can expire,
* two tenants running the *same vendor product* at different versions
  (``harbor`` on 4.2, ``pineridge`` on 4.3) with different labels/branding.

A control endpoint (``/__control/faults``) lets the test harness inject the
runtime conditions the brief cares about: interstitials, session expiry,
slow loads, transient 503s, hard 500s, permission denials, native dialogs
and unknown modals. It is *not* part of the "real" app and is never exposed
to the agent (the browser allowlist does not include it).

All data is synthetic.
"""

from __future__ import annotations

import html
import itertools
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class Tenant:
    id: str
    display: str
    version: str
    color: str
    # Per-version label differences — the kind of drift a vendor upgrade causes.
    labels: dict[str, str]


TENANTS: dict[str, Tenant] = {
    "harbor": Tenant(
        id="harbor",
        display="First Harbor Credit Union",
        version="4.2",
        color="#1f3a68",
        labels={
            "nav_inquiry": "Member Inquiry",
            "search_value": "Search Value:",
            "search_button": "Search",
            "balance_col": "Balance",
            "name_row": "Name:",
        },
    ),
    "pineridge": Tenant(
        id="pineridge",
        display="Pine Ridge Federal Credit Union",
        version="4.3",
        color="#2d5a27",
        labels={
            "nav_inquiry": "Member Lookup",
            "search_value": "Lookup Value:",
            "search_button": "Find",
            "balance_col": "Current Balance",
            "name_row": "Member Name:",
        },
    ),
}

# Demo teller credentials. Fake, local-only, and the agent never sees them in
# plaintext (they are injected from the secret store at action time).
USERS = {"teller01": "Harbor#Demo2026"}


@dataclass
class Share:
    share_id: str
    type: str
    description: str
    balance: str
    available: str


@dataclass
class Member:
    number: str
    name: str
    dob: str
    ssn_last4: str
    branch: str
    shares: list[Share]


def _members() -> dict[str, Member]:
    return {
        "10042": Member(
            "10042", "Dana Whitfield", "1981", "4417", "Harbor Main",
            [
                Share("S0001", "Share Savings", "Regular Savings", "$12,403.22", "$12,398.22"),
                Share("S0009", "Share Draft", "Everyday Checking", "$1,877.05", "$1,877.05"),
                Share("S0070", "Certificate", "12 Mo Certificate", "$5,000.00", "$0.00"),
            ],
        ),
        "10388": Member(
            "10388", "Marcus Oyelaran", "1974", "0932", "Westside",
            [
                Share("S0009", "Share Draft", "Everyday Checking", "$640.11", "$640.11"),
                Share("S0002", "Share Savings", "Regular Savings", "$88,120.40", "$88,120.40"),
            ],
        ),
        "20077": Member(
            "20077", "Priya Nandakumar", "1990", "7781", "Harbor Main",
            [Share("S0001", "Share Savings", "Regular Savings", "$305.00", "$300.00")],
        ),
    }


@dataclass
class Faults:
    """Runtime conditions injectable by the test harness (one-shot where noted)."""

    interstitial: bool = False            # one-shot "System Notice" page in the main frame
    expire_after: int | None = None       # session dies after N main-frame page loads
    slow_ms: int = 0                      # delay results/detail pages
    transient_503: int = 0                # next N results pages return "temporarily unavailable"
    error_500_on: str | None = None       # path fragment that returns a system error page
    deny_detail: bool = False             # member detail -> ACCESS DENIED
    hold_dialog: bool = False             # native confirm() on member detail
    survey_modal: bool = False            # unknown overlay on search results that blocks clicks

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class State:
    members: dict[str, dict[str, Member]] = field(
        default_factory=lambda: {t: _members() for t in TENANTS}
    )
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict)
    faults: Faults = field(default_factory=Faults)
    share_seq: itertools.count = field(default_factory=lambda: itertools.count(12))
    confirmations: itertools.count = field(default_factory=lambda: itertools.count(58213))


STATE = State()
COOKIE = "CO1SESS"

app = FastAPI(title="CoreOne Teller (mock)", docs_url=None, redoc_url=None)

# --------------------------------------------------------------------------- html helpers


def _page(body: str, *, title: str = "CoreOne Teller", extra_head: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<html><head><title>" + title + "</title>" + extra_head + "</head>"
        '<body bgcolor="#f4f1e8" topmargin="4" leftmargin="6">'
        '<font face="Verdana, Arial" size="2">' + body + "</font></body></html>"
    )


def _heading(text: str) -> str:
    return f'<table width="100%" cellpadding="2"><tr><td bgcolor="#d8d2bf"><font size="3"><b>{text}</b></font></td></tr></table><br>'


def _e(s: str) -> str:
    return html.escape(s, quote=True)


def _tenant(t: str) -> Tenant:
    if t not in TENANTS:
        raise KeyError(t)
    return TENANTS[t]


def _session(request: Request, t: str) -> dict[str, Any] | None:
    sid = request.cookies.get(COOKIE)
    sess = STATE.sessions.get(sid or "")
    if not sess or sess["tenant"] != t:
        return None
    return sess


def _expired_page(t: str) -> HTMLResponse:
    return _page(
        _heading("Session Ended")
        + '<table cellpadding="6"><tr><td><font color="#8a0000"><b>Your session has expired due to inactivity.</b></font></td></tr>'
        + f'<tr><td><a href="/t/{t}/login" target="_top">Sign On</a></td></tr></table>'
    )


def _main_guard(request: Request, t: str) -> tuple[dict[str, Any] | None, HTMLResponse | None]:
    """Common checks for main-frame content pages: auth, expiry, interstitial, 500s."""
    sess = _session(request, t)
    if sess is None:
        return None, _expired_page(t)
    f = STATE.faults
    if f.expire_after is not None:
        if f.expire_after <= 0:
            STATE.sessions.pop(request.cookies.get(COOKIE, ""), None)
            f.expire_after = None
            return None, _expired_page(t)
        f.expire_after -= 1
    if f.error_500_on and f.error_500_on in request.url.path:
        return None, HTMLResponse(
            "<html><body><h2>CoreOne System Error</h2><pre>ABEND S0C7 in MBRINQ02 at offset 0x1F4\n"
            "Transaction aborted. Reference: 8841-" + secrets.token_hex(2).upper() + "</pre></body></html>",
            status_code=500,
        )
    if f.interstitial and not request.url.path.endswith("/home"):
        f.interstitial = False
        nxt = request.url.path + ("?" + request.url.query if request.url.query else "")
        return None, _page(
            _heading("System Notice")
            + "<table cellpadding=6><tr><td>Scheduled maintenance: CoreOne will be unavailable Sunday 02:00–04:00 ET."
            + "<br>Please complete open transactions before that window.</td></tr>"
            + f'<tr><td><form action="/t/{t}/ack" method="get"><input type="hidden" name="next" value="{_e(nxt)}">'
            + '<input type="submit" value="Acknowledge"></form></td></tr></table>'
        )
    return sess, None


# --------------------------------------------------------------------------- auth + shell


@app.get("/")
def root() -> HTMLResponse:
    links = "".join(f'<li><a href="/t/{t.id}/login">{t.display} ({t.version})</a></li>' for t in TENANTS.values())
    return _page("<h3>CoreOne Teller mock — tenants</h3><ul>" + links + "</ul>")


@app.get("/t/{t}/login")
def login_form(t: str, err: str = "") -> HTMLResponse:
    tn = _tenant(t)
    msg = '<tr><td colspan="2"><font color="#8a0000">Invalid user ID or password.</font></td></tr>' if err else ""
    return _page(
        f'<table width="100%" bgcolor="{tn.color}" cellpadding="8"><tr><td><font color="white" size="4"><b>{tn.display}</b></font>'
        f'<br><font color="#dddddd" size="1">CoreOne Teller {tn.version}</font></td></tr></table><br><br>'
        '<center><table border="1" cellpadding="8" cellspacing="0" bgcolor="#ffffff"><tr><td>'
        f'<form method="post" action="/t/{t}/login"><table cellpadding="4">{msg}'
        '<tr><td><font size="2">User ID</font></td><td><input type="text" name="F_0001" size="16"></td></tr>'
        '<tr><td><font size="2">Password</font></td><td><input type="password" name="F_0002" size="16"></td></tr>'
        '<tr><td></td><td><input type="submit" value="Sign On"></td></tr>'
        "</table></form></td></tr></table></center>",
        title=f"{tn.display} - Sign On",
    )


@app.post("/t/{t}/login")
def login(t: str, F_0001: str = Form(""), F_0002: str = Form("")) -> Response:
    _tenant(t)
    if USERS.get(F_0001) != F_0002:
        return RedirectResponse(f"/t/{t}/login?err=1", status_code=303)
    sid = secrets.token_hex(12)
    STATE.sessions[sid] = {"tenant": t, "user": F_0001, "created": time.time()}
    resp = RedirectResponse(f"/t/{t}/app", status_code=303)
    resp.set_cookie(COOKIE, sid, path="/", httponly=True)
    return resp


@app.get("/t/{t}/logout")
def logout(t: str, request: Request) -> Response:
    STATE.sessions.pop(request.cookies.get(COOKIE, ""), None)
    return RedirectResponse(f"/t/{t}/login", status_code=303)


@app.get("/t/{t}/app")
def shell(t: str, request: Request) -> Response:
    tn = _tenant(t)
    if _session(request, t) is None:
        return RedirectResponse(f"/t/{t}/login", status_code=303)
    return HTMLResponse(
        f"<html><head><title>{tn.display} - CoreOne Teller</title></head>"
        '<frameset rows="58,*" border="0" frameborder="0">'
        f'<frame name="banner" src="/t/{t}/banner" scrolling="no">'
        '<frameset cols="180,*" border="1">'
        f'<frame name="nav" src="/t/{t}/nav">'
        f'<frame name="main" src="/t/{t}/home">'
        "</frameset></frameset></html>"
    )


@app.get("/t/{t}/banner")
def banner(t: str, request: Request) -> HTMLResponse:
    tn = _tenant(t)
    sess = _session(request, t)
    user = sess["user"].upper() if sess else "-"
    return HTMLResponse(
        f'<html><body bgcolor="{tn.color}" topmargin="0" leftmargin="0"><table width="100%" cellpadding="6"><tr>'
        f'<td><font face="Verdana" color="white" size="3"><b>{tn.display}</b></font></td>'
        f'<td align="right"><font face="Verdana" color="#dddddd" size="1">CoreOne Teller {tn.version} &nbsp; Operator: {user}</font></td>'
        "</tr></table></body></html>"
    )


@app.get("/t/{t}/nav")
def nav(t: str) -> HTMLResponse:
    tn = _tenant(t)
    items = [
        (f"/t/{t}/member/search", tn.labels["nav_inquiry"], "main"),
        (f"/t/{t}/share/new", "New Share Account", "main"),
        (f"/t/{t}/reports", "Reports", "main"),
        (f"/t/{t}/logout", "Sign Off", "_top"),
    ]
    rows = "".join(
        f'<tr><td><font size="2"><a href="{href}" target="{tgt}">{label}</a></font></td></tr>' for href, label, tgt in items
    )
    return _page('<table cellpadding="5" width="100%">' + rows + "</table>")


@app.get("/t/{t}/home")
def home(t: str, request: Request) -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    return _page(_heading("Teller Workstation") + "Select a function from the menu.")


@app.get("/t/{t}/ack")
def ack(t: str, next: str = "") -> Response:
    _tenant(t)
    target = next if next.startswith(f"/t/{t}/") else f"/t/{t}/home"
    return RedirectResponse(target, status_code=303)


@app.get("/t/{t}/reports")
def reports(t: str, request: Request) -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    return _page(_heading("Reports") + "EOD balancing, GL extracts, OFAC hits (not part of the demo).")


# --------------------------------------------------------------------------- member inquiry

# An overlay the automation has never seen: not in the app profile, blocks clicks.
_SURVEY = (
    '<div style="position:fixed;left:0;top:0;width:100%;height:100%;background:rgba(0,0,0,.45);z-index:10">'
    '<div style="margin:60px auto;width:320px;background:#fff;border:2px solid #555;padding:14px">'
    "<b>Quick survey</b><br>How satisfied are you with CoreOne today?<br><br>"
    '<span style="cursor:pointer;border:1px solid #888;padding:2px 8px" onclick="this.parentNode.parentNode.remove()">No thanks</span>'
    "</div></div>"
)


def _search_form(t: str, *, value: str = "", message: str = "") -> str:
    lb = _tenant(t).labels
    return (
        _heading(lb["nav_inquiry"])
        + f'<form action="/t/{t}/member/results" method="get"><table cellpadding="4">'
        + '<tr><td><font size="2">Search By:</font></td><td><select name="F_0107">'
        + '<option value="M" selected>Member Number</option><option value="S">SSN (last 4)</option><option value="N">Last Name</option>'
        + "</select></td></tr>"
        + f'<tr><td><font size="2">{lb["search_value"]}</font></td><td><input type="text" name="F_0108" size="14" value="{_e(value)}"></td></tr>'
        + f'<tr><td></td><td><input type="submit" value="{lb["search_button"]}"></td></tr>'
        + "</table></form>"
        + (f"<br>{message}" if message else "")
    )


@app.get("/t/{t}/member/search")
def member_search(t: str, request: Request) -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    return _page(_search_form(t))


@app.get("/t/{t}/member/results")
def member_results(t: str, request: Request, F_0107: str = "M", F_0108: str = "") -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    f = STATE.faults
    if f.slow_ms:
        time.sleep(f.slow_ms / 1000)
    if f.transient_503 > 0:
        f.transient_503 -= 1
        return HTMLResponse(
            _page(_heading("Service Unavailable") + "The host is temporarily unavailable (MQ timeout). Please try again.").body,
            status_code=503,
        )
    q = F_0108.strip()
    if F_0107 == "M" and not (q.isdigit() and 5 <= len(q) <= 10):
        return _page(_search_form(t, value=q, message='<font color="#b00000"><b>** Invalid member number format **</b></font>'))
    members = STATE.members[t]
    hits: list[Member] = []
    if F_0107 == "M":
        hits = [members[q]] if q in members else []
    elif F_0107 == "S":
        hits = [m for m in members.values() if m.ssn_last4 == q]
    elif F_0107 == "N":
        hits = [m for m in members.values() if m.name.lower().split()[-1] == q.lower()]
    if not hits:
        return _page(_search_form(t, value=q, message="<i>No matching records found.</i>"))
    modal = ""
    if f.survey_modal:
        f.survey_modal = False
        modal = _SURVEY
    rows = "".join(
        f'<tr><td><a href="/t/{t}/member/detail?id={m.number}">{m.number}</a></td><td>{m.name}</td><td>{m.branch}</td><td>Active</td></tr>'
        for m in hits
    )
    return _page(
        modal
        + _search_form(t, value=q)
        + '<table border="1" cellspacing="0" cellpadding="4" width="90%">'
        + '<tr bgcolor="#d8d2bf"><td><b>Member #</b></td><td><b>Name</b></td><td><b>Branch</b></td><td><b>Status</b></td></tr>'
        + rows
        + "</table>"
    )


@app.get("/t/{t}/member/detail")
def member_detail(t: str, request: Request, id: str = "") -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    f = STATE.faults
    if f.slow_ms:
        time.sleep(f.slow_ms / 1000)
    if f.deny_detail:
        return _page(
            _heading("ACCESS DENIED")
            + f'<font color="#8a0000">ACCESS DENIED: operator {sess["user"].upper()} lacks entitlement MBR-INQ-DETAIL.</font>'
        )
    lb = _tenant(t).labels
    m = STATE.members[t].get(id)
    if m is None:
        return _page(_heading("Member Detail") + "<i>No matching records found.</i>")
    info = [
        ("Member Number:", m.number),
        (lb["name_row"], m.name),
        ("Date of Birth:", f"**/**/{m.dob}"),
        ("SSN:", f"***-**-{m.ssn_last4}"),
        ("Branch:", m.branch),
    ]
    info_html = "".join(f'<tr><td bgcolor="#ebe6d6">{k}</td><td>{v}</td></tr>' for k, v in info)
    share_rows = "".join(
        f"<tr><td>{s.share_id}</td><td>{s.type}</td><td>{s.description}</td>"
        f'<td align="right">{s.balance}</td><td align="right">{s.available}</td></tr>'
        for s in m.shares
    )
    head = ""
    if f.hold_dialog:
        f.hold_dialog = False
        head += "<script>window.onload=function(){confirm('This member has a pending hold on one or more shares. Continue?');}</script>"
    return _page(
        _heading("Member Detail")
        + '<table border="1" cellspacing="0" cellpadding="4">'
        + info_html
        + "</table><br>"
        + '<table border="1" cellspacing="0" cellpadding="4" width="95%">'
        + '<tr bgcolor="#d8d2bf"><td><b>Share ID</b></td><td><b>Type</b></td><td><b>Description</b></td>'
        + f'<td><b>{lb["balance_col"]}</b></td><td><b>Available</b></td></tr>'
        + share_rows
        + "</table><br>"
        + f'<a href="/t/{t}/share/new?member={m.number}">Open New Share</a>',
        extra_head=head,
    )


# --------------------------------------------------------------------------- new share (irreversible flow)

SHARE_TYPES = ["Money Market", "Certificate", "Holiday Club"]


@app.get("/t/{t}/share/new")
def share_new(t: str, request: Request, member: str = "", err: str = "") -> HTMLResponse:
    sess, early = _main_guard(request, t)
    if early:
        return early
    opts = "".join(f"<option>{s}</option>" for s in SHARE_TYPES)
    msg = f'<font color="#b00000"><b>** {_e(err)} **</b></font><br>' if err else ""
    return _page(
        _heading("New Share Account")
        + msg
        + f'<form action="/t/{t}/share/review" method="get"><table cellpadding="4">'
        + f'<tr><td>Member Number:</td><td><input type="text" name="F_0300" size="12" value="{_e(member)}"></td></tr>'
        + f'<tr><td>Share Type:</td><td><select name="F_0301"><option value="">-- select --</option>{opts}</select></td></tr>'
        + '<tr><td>Nickname:</td><td><input type="text" name="F_0302" size="20"></td></tr>'
        + '<tr><td>Opening Deposit:</td><td><input type="text" name="F_0303" size="10"></td></tr>'
        + '<tr><td></td><td><input type="submit" value="Continue"></td></tr>'
        + "</table></form>"
    )


@app.get("/t/{t}/share/review")
def share_review(
    t: str, request: Request, F_0300: str = "", F_0301: str = "", F_0302: str = "", F_0303: str = ""
) -> Response:
    sess, early = _main_guard(request, t)
    if early:
        return early
    m = STATE.members[t].get(F_0300.strip())
    err = ""
    if m is None:
        err = "Member not found"
    elif F_0301 not in SHARE_TYPES:
        err = "Share type is required"
    else:
        try:
            if float(F_0303.replace("$", "").replace(",", "") or "0") < 0:
                err = "Opening deposit must be positive"
        except ValueError:
            err = "Opening deposit must be numeric"
    if err:
        q = urlencode({"member": F_0300, "err": err})
        return RedirectResponse(f"/t/{t}/share/new?{q}", status_code=303)
    assert m is not None
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_e(v)}">'
        for k, v in {"F_0300": F_0300, "F_0301": F_0301, "F_0302": F_0302, "F_0303": F_0303}.items()
    )
    return _page(
        _heading("Review New Share")
        + '<table border="1" cellspacing="0" cellpadding="4">'
        + f"<tr><td>Member</td><td>{m.number} - {m.name}</td></tr>"
        + f"<tr><td>Share Type</td><td>{_e(F_0301)}</td></tr>"
        + f"<tr><td>Nickname</td><td>{_e(F_0302)}</td></tr>"
        + f"<tr><td>Opening Deposit</td><td>{_e(F_0303)}</td></tr>"
        + "</table><br>This action creates a new share on the member record and cannot be undone from this screen.<br><br>"
        + f'<form action="/t/{t}/share/open" method="post">{hidden}'
        + '<input type="submit" value="Confirm &amp; Open"> &nbsp; '
        + f'<a href="/t/{t}/share/new?member={quote(F_0300)}">Cancel</a></form>'
    )


@app.post("/t/{t}/share/open")
def share_open(
    t: str,
    request: Request,
    F_0300: str = Form(""),
    F_0301: str = Form(""),
    F_0302: str = Form(""),
    F_0303: str = Form(""),
) -> Response:
    sess, early = _main_guard(request, t)
    if early:
        return early
    m = STATE.members[t].get(F_0300)
    if m is None or F_0301 not in SHARE_TYPES:
        return RedirectResponse(f"/t/{t}/share/new?err=Invalid+request", status_code=303)
    share_id = f"S{next(STATE.share_seq):04d}"
    amt = F_0303 if F_0303.startswith("$") else f"${F_0303}"
    m.shares.append(Share(share_id, F_0301, F_0302 or F_0301, amt, amt))
    conf = f"CNF-{next(STATE.confirmations)}"
    return _page(
        _heading("Share Opened")
        + '<table border="1" cellspacing="0" cellpadding="4">'
        + f"<tr><td>Status</td><td><b>Share opened successfully</b></td></tr>"
        + f"<tr><td>New Share ID</td><td>{share_id}</td></tr>"
        + f"<tr><td>Confirmation #</td><td>{conf}</td></tr>"
        + "</table>"
    )


# --------------------------------------------------------------------------- harness control (not part of the "app")


@app.get("/__control/faults")
def get_faults() -> JSONResponse:
    return JSONResponse(STATE.faults.to_dict())


@app.post("/__control/faults")
async def set_faults(request: Request) -> JSONResponse:
    body = await request.json()
    fresh = Faults()
    for k, v in body.items():
        if not hasattr(fresh, k):
            return JSONResponse({"error": f"unknown fault {k}"}, status_code=400)
        setattr(fresh, k, v)
    STATE.faults = fresh
    return JSONResponse(fresh.to_dict())


@app.post("/__control/reset")
def reset() -> JSONResponse:
    global STATE
    STATE = State()
    return JSONResponse({"ok": True})
