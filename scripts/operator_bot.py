"""Scripted operator for reproducible evidence.

Drives the operator console's HTTP API exactly as the console page does when
a person clicks its buttons: wait for an intervention, (optionally) take
control of the live session, click/type in it, then hand control back with a
resolution. Used so the handoff in /evidence is reproducible; a person can do
the same thing in the browser at http://127.0.0.1:8802/.

    python scripts/operator_bot.py --claim --click 562,190 --release retry --notes "closed survey popup"
    python scripts/operator_bot.py --release approve --notes "member consent on file"
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

p = argparse.ArgumentParser()
p.add_argument("--console", default="http://127.0.0.1:8802")
p.add_argument("--operator", default="op:jlee")
p.add_argument("--claim", action="store_true", help="take control of the live session first")
p.add_argument("--click", action="append", default=[], help="x,y in page pixels (as seen on the console screenshot)")
p.add_argument("--type", dest="text")
p.add_argument("--release", required=True, choices=["approve", "deny", "retry", "skip", "continue", "abort"])
p.add_argument("--notes", default=None)
p.add_argument("--timeout", type=int, default=300)
p.add_argument("--think", type=float, default=1.5, help="seconds between actions, like a person")
a = p.parse_args()

deadline = time.time() + a.timeout
iv = None
while time.time() < deadline:
    try:
        st = httpx.get(f"{a.console}/api/state", timeout=2).json()
        iv = st.get("intervention")
        if iv and iv["status"] == "open":
            break
    except httpx.HTTPError:
        pass
    time.sleep(0.5)
else:
    sys.exit("no intervention appeared")

print(f"[operator] intervention {iv['id']} ({iv['kind']}): {iv['reason']}")


def post(op: str, **body) -> None:
    httpx.post(f"{a.console}/api/{op}", json={"operator": a.operator, **body}, timeout=5).raise_for_status()
    time.sleep(a.think)


time.sleep(a.think)
if a.claim:
    post("claim")
    print("[operator] took control")
for c in a.click:
    x, y = (float(v) for v in c.split(","))
    post("click", x=x, y=y)
    print(f"[operator] click {x},{y}")
if a.text:
    post("type", text=a.text)
post("release", resolution=a.release, notes=a.notes)
print(f"[operator] released: {a.release}")
