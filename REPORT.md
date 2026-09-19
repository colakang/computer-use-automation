# Design report

The system follows the brief's through-line: the model discovers the flow, the artifact becomes a reusable capability, and deterministic replay is how production invokes it. I implemented it against a deliberately hostile legacy mock, **CoreOne Teller**, which has framesets, table layout, no `<label>`s, no ids and opaque field names. Two tenants run different vendor versions of it. The evidence has two real Claude discovery runs and 18 replay scenarios, and every replay scenario is reproducible without a model.

## 1. Architecture

```
          ┌──────────── contract (human) ─────────────┐
goal spec ┤                                            ├─► Capability artifact ──► store (files, semver, hash-bound approval)
          └─► DiscoveryAgent ─► trace ─► Recorder ─────┘         │ + overlays (vendor version / tenant)
                 │ LLM (claude-cli | anthropic)                  ▼
                 │                                        Replayer (no LLM) ─► ReplayResult
                 ▼                                                │
   ┌───────────────── one live session ────────────────────────────┴───────────┐
   │ Surface (web.py + perception.js) ◄── Policy Gate ◄── every action         │
   │ ControlChannel: AUTOMATION ⇄ AWAITING_HUMAN ⇄ HUMAN  ── operator console   │
   │ RunLog (redacted JSONL, masked screenshots)   AppProfile conditions        │
   └────────────────────────────────────────────────────────────────────────────┘
```

**Key decisions**

- **Single process and synchronous, with one owner per live session.** Playwright objects live on the automation thread. The operator console only enqueues commands, and the owner thread executes them *only while the human holds control*. The benefit is that the control-transfer model cannot race. The cost is that there is no concurrency inside a session, and a production deployment would run one worker per session. I chose not to build a queue or service layer, per §7 of the brief.
- **Perception is our own ~400 lines of JS, not Playwright selectors or a vision model.** It builds an accessibility-style view: role, accessible name, *visual caption* ("the box next to `Search Value:`") and *table coordinates* (column header × a row identified by meaning). The same functions serve three purposes: the model's observation, recording (`describe`) and replay (`resolve`). A strategy that was unique when recorded therefore means the same thing when replayed. I rejected screenshot-plus-coordinates as the primary method: it cannot replay deterministically, and a pixel is not an auditable identity for "the Balance of Share Savings".
- **The model sees text, not pixels.** It gets a structured snapshot of every frame with refs, a redacted action history, and the *names* of secrets. It returns one JSON action per step, and each call is stateless. Being stateless means a run can pause for a human for ten minutes and resume without stale conversation state. It also means `claude -p` and the Messages API are interchangeable behind a single `complete(system, user)` seam.
- **The contract is human-authored and only the body is discovered.** `goals/*.yaml` declare inputs, outputs, types and sensitivity. The model decides *how*, not *what a calling agent may ask for*, because that is a product decision.

## 2. Artifact schema

`src/cua/schema.py`; example: [`capabilities/member.savings_balance.read/0.1.0.json`](capabilities/member.savings_balance.read/0.1.0.json). `cua show` prints the review view.

| part | contents | why |
|---|---|---|
| contract | `inputs` (type, regex, enum, **sensitivity**), `outputs` (type, sensitivity), `outcomes` (business codes) | Exported as a function-calling tool schema (`tool_schema()`). Inputs are validated before the UI is touched. Sensitivity drives redaction. |
| body | `steps[]`: `action`, `target`, `value` (`{input}` / `{secret}` / `{literal}`, never an inline secret), `risk` (read / reversible / irreversible), `idempotent`, `phase` (auth / main), `expect` checkpoint, `provenance` (model / human) | Everything replay needs to act, verify and recover *without judgement*. `risk` and `idempotent` decide what may be retried. `phase` marks the sign-on prefix. |
| target | `frame` + an **ordered list of strategies**: `role_name` → `label` → `table_cell` → `attr` → `css` | Only strategies that were **unique at record time** are kept. Replay takes the first strategy that matches **exactly one** element. Zero or several matches never fall through to a guess. |
| checkpoints | each navigation's resulting URL (parameterized: `/member/detail?id={{member_id}}`), plus a success checkpoint (URL and a success element) | Proves each click *worked*, rather than just that it didn't throw. |
| governance | `version` (semver), `provenance` (discovery run, model, tenant), `approval` bound to `content_hash()` | Reviewable like code. An edited artifact silently loses its approval. |

The recorder parameterizes any literal equal to an input example: in values, in locator fields (`link "{{member_id}}"`) and in URL checkpoints, where it compares *decoded* query values. Before writing, it lints the artifact and refuses to save if any secret or observed sensitive value would be persisted.

**Environment knowledge stays out of the artifact.** Conditions like "session expired", "System Notice", "No matching records found" and "ACCESS DENIED" are properties of the *vendor product*, so they live once in `config/apps/coreone-teller.yaml`. Every capability on every tenant inherits them. The capability maps a profile condition to its own business vocabulary (`no_records → RECORD_NOT_FOUND`).

## 3. Determinism & error handling

Replay is a small state machine per step (`replay.py`):

> detect conditions → resolve target (unique) → policy gate → act → await post-condition (still detecting)

Waits are bounded polls, never sleeps. Resolution also refuses to run against a document whose `readyState` is not `complete`.

**Result contract:** `success{outputs}` · `business_outcome{code}` · `failure{kind, step, intent, expected, observed, retryable, evidence[]}` · `rejected` (the call itself was invalid). Every result also reports `recoveries[]`, `drift[]` and `interventions[]`, so a success that needed help is distinguishable from a clean one.

| class | examples (all in `evidence/REPLAYS.md`) | response |
|---|---|---|
| **business** (a legitimate answer) | RECORD_NOT_FOUND, INVALID_INPUT, VALIDATION_ERROR | Terminal; returned to the caller; not an error |
| **recoverable** | System Notice → dismiss · session expired → sign on again and restart from the entry · transient 503 → bounded reload · known native dialog → accept · slow load → wait | Handled in place, bounded by `max_attempts`, recorded in `recoveries[]` |
| **hard failure** | app_error, permission_denied, action_blocked, target_not_found / ambiguous / drifted, checkpoint_failed, unexpected_dialog, approval_required, recovery_exhausted, session_lost_after_commit | Stop with a masked screenshot and a structure-only DOM snapshot; optionally escalate |
| **rejected** | invalid_input, not_approved (draft or tampered) | Nothing executed |

Some rules I'd defend in review:

- **Recovery never repeats a commit.** Reload is only allowed after an idempotent step. Re-auth restarts from the entry point, sign-on included. That is safe *only because* nothing has committed yet: framesets rarely offer deep links that would let replay resume mid-flow. If the session expires *after* an irreversible step, the result is `session_lost_after_commit` and a human is needed, because the outcome on the host is unknown.
- **Drift is reported, not hidden.** When a fallback strategy resolves, the result carries a drift signal. **Reads and irreversible actions refuse structural-only resolution** (`target_drifted`). On the 4.3 tenant with overlays disabled, the css path still found *a* cell, but returning a balance by position is how a wrong number reaches a member. A fallback also gets a 1s grace window for the preferred strategy to appear. I found this race while generating evidence: a half-loaded detail page let the css path match before the header row existed. I fixed it with the `readyState` guard and the grace window, and `evidence/STABILITY.md` shows repeated runs.
- **Permission denied is a hard failure, not a business outcome.** It says the *service operator* lacks an entitlement. That is an ops or config problem, not a fact about the member.

## 4. Heterogeneity & multi-tenant

**The surface seam** (`surface/base.py`). Everything above it speaks in observations with refs, element info, `Target`/strategies, and primitive actions. A **desktop** surface implements the same protocol on UI Automation (Windows) or AX (macOS):

| strategy | desktop equivalent |
|---|---|
| `role_name` | ControlType + Name |
| `label` | the preceding static text |
| `attr` | AutomationId |
| `table_cell` | Grid/Table patterns |
| `frame` | a window path |

A **3270/terminal** surface maps fields to screen-buffer positions and labels. The artifact schema and the replay engine do not change; each surface declares which strategy kinds it supports. The legacy-web case is already what I built against.

**Pixel-only surfaces** (Citrix/RDP-published apps, owner-drawn clients with an empty UIA tree) get a `VisionSurface` behind the same protocol. A screen parser turns each screenshot into elements with role, text, visual caption, table position, enabled state and bounding box. The parser can be a VLM or a dedicated UI-element detector plus OCR. Four rules keep this compatible with deterministic replay:

- **Targets stay semantic.** `role_name`, `label` and `table_cell` resolve against the parsed elements exactly as they do against the DOM. The bounding box only says *where to deliver* the click, never *which control* it is. So an artifact recorded at 1280×800 still applies at 1920×1080, under another theme, or with the window moved.
- **The determinism boundary is *decisions*, not *models*.** Replay may use a model to *perceive*, the same way it already uses a browser engine to render. It may never use a model to *decide* what to do next. The parser is pinned (model, version and prompt hash recorded in the artifact's `app` block). Its output passes the same gates: exactly one match or fail, and verify after every act.
- **Cost and latency.** A parse costs seconds, while a DOM query costs milliseconds. Stable enterprise UIs make parses highly cacheable by screen fingerprint, and replay only needs to re-parse the region it is about to touch.
- **Parser failure modes.** The parser can hallucinate controls, merge adjacent ones, misread disabled state, or miss content that is scrolled off-screen. These are caught by cross-checking against OCR text, by the uniqueness rule, and by post-action checkpoints. They are measured by a robustness harness that perturbs scale, layout and theme (alongside the fault injection already in `mockbank`) and gates approval on the pass rate.

Where it runs: one dedicated VM per digital worker inside the institution's VDI, logged in as a least-privilege service account (never a teller's session), with an in-VM sidecar exposing whichever channel exists (DOM, UIA or pixels). Human takeover becomes shadowing that same VM session. Screenshots are PII, so they are masked before any hosted model sees them, or the parser runs on-prem.

**Reuse across tenants** is layered as base capability → *vendor-version overlay* → *tenant overlay*:

- An overlay can only re-target controls and adjust checkpoints. It cannot add steps, change the contract or lower risk. So the calling agent sees **one tool schema across every tenant**, and an approved base plus a reviewed overlay is still the same capability.
- Tenants map to vendor versions in `config/tenants/*.yaml`, and each vendor version needs **one** overlay shared by all its tenants. In the demo, `harbor` runs 4.2 and `pineridge` runs 4.3. The 4.3 overlay patches five relabelled controls (`Member Lookup`, `Lookup Value:`, `Find`, `Current Balance`, `Member Name:`).
- **Detecting drift:** a tenant whose version isn't in `app.versions` and has no overlay gets a warning and a best-effort attempt. Fallback resolution emits drift signals, and semantic reads refuse to guess. Those signals point to exactly which steps an overlay needs, which in production is the trigger for a bounded, reviewed re-discovery of just those steps.

## 5. Escalation & handoff

**Detecting "stuck":**

- In **discovery**: the same action on an unchanged screen three times, three invalid model replies, an explicit `give_up`, or a step or time budget exhausted.
- In **replay**: any hard failure when `--on-failure escalate` is set. Policy violations are the exception; they are never handed to a human to work around.
- **Risk gates** escalate too: an irreversible action in discovery, and in replay with `--irreversible escalate`.

**Control model** (`handoff.py`): a `ControlChannel` holds `state ∈ {automation, awaiting_human, human}` and a `holder`. Automation must call `require_automation()` before every action. The intervention request carries the capability or goal, the step and its intent, the reason, expected and observed state, redacted frame locations, recoveries so far and a masked screenshot. It is written to `interventions/<id>.json` (a stand-in for a queue or pager) and to the run evidence.

**Taking control:** the operator claims the **same browser session**, either through the console's live masked screenshot (click, type, keys) or the headed window. Console input is ignored until control has been claimed. Every DOM event in every frame is captured by the injected listener and described with the *recorder's* strategies (`button "No thanks"`, not `(562,190)`). Typed values are never captured, only field and length.

**Handing back** uses a typed resolution:

| context | resolutions |
|---|---|
| failure | `retry` (re-run the step), `skip` ("I did it"; replay still verifies that step's post-condition), `abort` |
| approval | `approve` / `deny` |
| discovery | `continue`, with the human's actions added to the model's history and to the trace as `provenance: human` steps |

Every transition is logged with actor and reason. The evidence shows both kinds of handoff: `handoff-modal` (take over, fix, retry → success) and `share-approval-handoff`, plus the approval gate during the real `member.share.open` discovery.

## 6. Safety

- **Allowlist.** The allowlist is in `config/policy.yaml`: action types, path regexes relative to the tenant base, denied paths, and irreversible-control rules. It is enforced in two places. `Gate.check` runs before every action in discovery *and* replay, and inspects where a link or submit leads *before* clicking it. A browser route filter then blocks foreign origins and off-list document loads as a backstop. A policy denial is explained to the model, and repeated attempts become "stuck".
- **Irreversible actions.** A control is classified irreversible by role, name or form target (a `POST` to `/share/open` is caught even under a harmless label). Live classification can only *raise* the recorded risk, never lower it. Discovery pauses for a human; replay requires per-call authorization or escalates. I rejected *blocking* outright, because then opening an account could never be automated. I also rejected *flag-only*, because then a mis-recorded step could commit unattended.
- **Redaction:**
  1. Secrets live only in the secret store. The model never sees them, and every secret is registered with the redactor at session start.
  2. Declared inputs and outputs are redacted by sensitivity: exact values, and structurally in `result.json`.
  3. Patterns catch SSN, DOB, money, email and card numbers.
  4. Screenshots are masked *in the page before capture*: data-table cells, `Caption:` values and pattern matches.
  5. Failure snapshots are structure-only: `‹text:14›`, not the name.
  6. Logs describe cells by table position, never by content.
  7. Playwright traces are not used at all, because they capture typed credentials and full DOM.
  8. A test scans all persisted evidence for known sensitive values.
- **Limits:**
  - During discovery the **model sees page data**. Discovery must run on sandbox or synthetic members, or a de-identified test tenant; production replays never call the model.
  - Undeclared free-text PII is caught by structural masking in screenshots and snapshots, but not in arbitrary log strings.
  - The operator console is unauthenticated and bound to localhost.
  - The allowlist is URL-based, so a same-URL action with a different effect relies on the irreversible rules.

## 7. Cuts

**Mocked deliberately, with the seams real:**

- **The target app.** A real bank system was off-limits, and I needed fault injection anyway.
- **The operator console**, which is a bare page. The control model underneath it is real.
- **`operator_bot.py`**, which drives the console API the way the page does so the evidence can be reproduced; a person can do the same thing at `:8802`.
- **The desktop surface**, design only.
- **The registry**, which is files in git.

**Left out:**

- Authentication and authorization for the console and interventions.
- Webhook or pager routing.
- Promoting human steps into a new artifact version automatically (they are captured as `provenance: human` trace entries in discovery, but not replayed from replay escalations).
- The pixel-only `VisionSurface` (designed above, not built).
- Parallel sessions.

**Next, in order:**

1. **Bounded assisted re-discovery on drift.** When replay reports `target_drifted` on step N, let the model propose new strategies for *that step only*, policy-checked, and emit an overlay for review.
2. A capability registry service with approval workflow and per-tenant rollout (canary tenants first).
3. Real operator routing (queue, SLAs, authn) and a streaming co-browse console.
4. A `VisionSurface` (pinned screen parser + OCR cross-check) and a desktop `Surface` on UI Automation, both reusing `strategies` as-is, plus a perturbation harness (scale/layout/theme × fault injection) whose pass rate gates approval.
5. An approval gate driven by a confidence score from `scripts/stability.py`-style replays across tenants.
