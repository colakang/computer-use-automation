# Evidence

Everything in this directory went through the redactor before it reached disk:

- Logs contain no credentials, member names, balances or SSNs.
- Screenshots are masked in the page before capture.
- Failure snapshots contain structure only.

The mock's data is synthetic anyway, but the pipeline treats it as if it were real. `tests/test_replay.py::test_nothing_sensitive_is_persisted` checks this.

## 1. Discovery runs (real LLM: Claude Sonnet 5 via `claude -p`)

| run | goal | result |
|---|---|---|
| [`20260918T232717Z-discovery-member-savings_balance-read`](runs/20260918T232717Z-discovery-member-savings_balance-read) | Sign on, look up member 10042, read the Share Savings balance and the member's name | Completed in 10 model decisions (~36 s). Produced [`member.savings_balance.read@0.1.0`](../capabilities/member.savings_balance.read/0.1.0.json) |
| [`20260918T232920Z-discovery-member-share-open`](runs/20260918T232920Z-discovery-member-share-open) | Open a Holiday Club share for member 20077 and read the confirmation | Completed in 13 decisions. The agent proposed **Confirm & Open**, the policy gate classified it as irreversible and raised an **approval intervention**, and an operator approved it through the console (`intervention-*.json`, `screens/001-intervention-approval.png`). Produced [`member.share.open@0.1.0`](../capabilities/member.share.open/0.1.0.json) |

What a discovery run directory contains:

- **`log.jsonl`** has, for every step:
  - `observe`: an observation digest and the frame URLs. The full observation is *not* persisted, because it contains member data.
  - `decide`: the model's action, its one-line reason, where any typed value came from (`input` / `secret` / `literal`, never the value itself), and token counts.
  - `policy`: the gate's verdict and the risk class.
  - `act`: the outcome, including which frames navigated where.
- **`artifact.json`**: what the recorder produced from the trace.
- **`screens/`**: masked screenshots.

## 2. Replay runs (no LLM)

**[REPLAYS.md](REPLAYS.md)** indexes 18 scenarios:

- **Happy path.**
- **Business outcomes:** not found, and write-flow validation.
- **A contract rejection:** bad input.
- **Five recoverable conditions:** interstitial, session expiry, transient 503, slow load, native dialog.
- **Hard failures:** host error, permission denied, blocking popup.
- **Handoff:** the **live-session handoff** that turns the blocking popup into a success.
- **Cross-tenant:** replay on a 4.3 tenant *without* the overlay (drift is detected, and the engine refuses to read a balance by position) and *with* it (clean success).
- **The irreversible step:** blocked, pre-authorized, and approved by a human at the step.

Each run has `scenario.txt` (the exact command), `log.jsonl`, `result.json`, `screens/` and, where relevant, `failure-snapshot.txt` and `intervention-*.json`.

The handoff scenarios use `scripts/operator_bot.py`. It is a scripted operator that calls the console's HTTP API exactly as the console page does, so the handoff is reproducible. The human's click is captured in the page by the same listener as a manual click, and it is recorded as `button "No thanks"` in `result.json → interventions[].human_actions`.

**[VISION.md](VISION.md)** covers the pixel-only prototype. The same approved artifact is replayed through `--surface vision`: screenshots in, mouse and keyboard out, and a pinned VLM screen parser in between. It shows the happy path, a business outcome read from on-screen text, the System Notice recovery, and a refusal to click a control that the parser reports as covered by an overlay.

**[STABILITY.md](STABILITY.md)**: 20 runs of each of four cases, giving a pass rate, timing spread and drift count. It exists because generating this evidence surfaced a load-timing race (see REPORT.md §3).

Regenerate: `make evidence` (replays) · `make stability` · `uv run cua discover goals/<spec>.yaml --evidence evidence/runs` (needs a model).
