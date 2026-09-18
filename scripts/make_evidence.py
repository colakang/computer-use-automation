"""Regenerate the replay evidence in evidence/runs and the index in evidence/REPLAYS.md.

Each scenario is a plain `cua replay` invocation (plus, for handoff scenarios,
the scripted operator driving the console API). Nothing here calls an LLM.

    uv run python scripts/make_evidence.py            # all scenarios
    uv run python scripts/make_evidence.py happy drift # a subset
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVID = ROOT / "evidence"
BAL = "member.savings_balance.read"
SHARE = "member.share.open"
SHARE_IN = ["-i", "member_id=10042", "-i", "share_type=Certificate", "-i", "nickname=Rainy Day", "-i", "opening_deposit=500.00"]

# name: (what it demonstrates, cua replay args, operator-bot args or None)
SCENARIOS: dict[str, tuple[str, list[str], list[str] | None]] = {
    "happy": ("Happy path, approved artifact, no LLM", [BAL, "-t", "harbor", "-i", "member_id=10042"], None),
    "not-found": ("Business outcome: member does not exist", [BAL, "-t", "harbor", "-i", "member_id=99999"], None),
    "invalid-input": ("Rejected before touching the UI: input violates contract", [BAL, "-t", "harbor", "-i", "member_id=12ab"], None),
    "interstitial": ("Recoverable: System Notice dismissed via app-profile handler",
                     [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "interstitial=true"], None),
    "session-expiry": ("Recoverable: session expired mid-flow → re-authenticate, restart main phase",
                       [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "expire_after=1"], None),
    "transient-503": ("Recoverable: transient host outage → bounded reload (idempotent step)",
                      [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "transient_503=1"], None),
    "slow": ("Slow load (3s) absorbed by bounded waits; no fixed sleeps",
             [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "slow_ms=3000"], None),
    "known-dialog": ("Native confirm() recognised by app profile and accepted",
                     [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "hold_dialog=true"], None),
    "app-error": ("Hard failure: host abend page → app_error with evidence",
                  [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "error_500_on=/member/detail"], None),
    "permission-denied": ("Hard failure: operator lacks entitlement → permission_denied",
                          [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "deny_detail=true"], None),
    "unknown-modal": ("Hard failure: unknown overlay blocks the click (no escalation configured)",
                      [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "survey_modal=true"], None),
    "handoff-modal": ("HITL: same failure, escalated → operator takes the live session, closes popup, hands back → success",
                      [BAL, "-t", "harbor", "-i", "member_id=10042", "--inject", "survey_modal=true", "--on-failure", "escalate"],
                      ["--claim", "--click", "562,190", "--release", "retry", "--notes", "closed unexpected survey popup"]),
    "drift-no-overlay": ("Cross-tenant: 4.2 artifact on a 4.3 tenant, overlays disabled → drift signals, then refuses to read a balance by position",
                         [BAL, "-t", "pineridge", "-i", "member_id=10042", "--no-overlays"], None),
    "tenant-overlay": ("Cross-tenant: same artifact + reviewed 4.3 overlay → success, zero drift",
                       [BAL, "-t", "pineridge", "-i", "member_id=10042"], None),
    "share-blocked": ("Irreversible step without authorization → stops before committing (approval_required)",
                      [SHARE, "-t", "harbor", *SHARE_IN], None),
    "share-validation": ("Business outcome from app validation on a write flow (unknown member)",
                         [SHARE, "-t", "harbor", "-i", "member_id=55555", "-i", "share_type=Certificate",
                          "-i", "nickname=Rainy Day", "-i", "opening_deposit=500.00", "--irreversible", "authorized"], None),
    "share-authorized": ("Irreversible step pre-authorized by the caller → commits, returns confirmation",
                         [SHARE, "-t", "harbor", *SHARE_IN, "--irreversible", "authorized"], None),
    "share-approval-handoff": ("Irreversible step escalated to a human approver at the step → approved → commits",
                               [SHARE, "-t", "harbor", *SHARE_IN, "--irreversible", "escalate"],
                               ["--release", "approve", "--notes", "member consent confirmed by phone"]),
}


def run(name: str) -> dict:
    desc, args, bot = SCENARIOS[name]
    bot_proc = None
    if bot:
        bot_proc = subprocess.Popen([sys.executable, str(ROOT / "scripts/operator_bot.py"), *bot],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    p = subprocess.run(["uv", "run", "cua", "replay", *args, "--evidence", "evidence/runs"],
                       cwd=ROOT, capture_output=True, text=True)
    if bot_proc:
        bot_out = bot_proc.communicate(timeout=30)[0]
        print(bot_out.strip())
    out = json.loads(p.stdout[p.stdout.index("{"):])
    run_dir = ROOT / out["evidence"]
    (run_dir / "scenario.txt").write_text(f"{name}: {desc}\ncommand: cua replay {' '.join(args)}\n"
                                          + (f"operator: scripts/operator_bot.py {' '.join(bot)}\n" if bot else ""))
    detail = (out.get("outcome") or {}).get("code") or (out.get("failure") or {}).get("kind") or ""
    print(f"{name:<24} {out['status']:<17} {detail:<22} {time.time() - t0:5.1f}s  {out['evidence']}")
    return {"name": name, "desc": desc, "args": args, "bot": bot, "result": out}


def main() -> None:
    names = sys.argv[1:] or list(SCENARIOS)
    rows = [run(n) for n in names]
    if sys.argv[1:]:
        return
    lines = [
        "# Replay evidence index",
        "",
        "Generated by `uv run python scripts/make_evidence.py`. Every row is a deterministic replay (no LLM).",
        "Each run directory has `log.jsonl` (structured, redacted), `result.json` (the result contract, redacted),",
        "`screens/` (masked screenshots) and, on failure, `failure-snapshot.txt` (structure-only DOM text).",
        "",
        "| scenario | demonstrates | status | outcome / failure | recoveries | drift | human | run |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        o = r["result"]
        detail = (o.get("outcome") or {}).get("code") or (o.get("failure") or {}).get("kind") or "—"
        rec = ", ".join(f'{x["condition"]}→{x["handler"]}' for x in o["recoveries"]) or "—"
        human = ", ".join(f'{i["kind"]}:{i["resolution"]}' for i in o["interventions"]) or "—"
        run_rel = Path(o["evidence"]).relative_to("evidence")
        lines.append(f"| `{r['name']}` | {r['desc']} | **{o['status']}** | {detail} | {rec} | {len(o['drift'])} | {human} | [{run_rel.name}]({run_rel}) |")
    (EVID / "REPLAYS.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {EVID / 'REPLAYS.md'}")


if __name__ == "__main__":
    main()
