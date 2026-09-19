# Pixel-only replay evidence (VisionSurface prototype)

The *same approved artifact* as REPLAYS.md, recorded on the DOM surface, replayed with no DOM access:
screenshots in, mouse/keyboard out, a pinned VLM screen parser in between (pin logged as `vision.parser`).
URL checkpoints cannot be observed on pixels and are listed as `unverified_checkpoints` in each result.

| scenario | demonstrates | status | outcome / failure | recoveries | drift | human | run |
|---|---|---|---|---|---|---|---|
| `vision-happy` | Pixels only: DOM-recorded artifact replayed from screenshots + mouse/keyboard | **success** | — | — | 0 | — | [20260919T012108Z-replay-member-savings_balance-read-vision](runs/20260919T012108Z-replay-member-savings_balance-read-vision) |
| `vision-not-found` | Pixels only: business outcome detected from on-screen text | **business_outcome** | RECORD_NOT_FOUND | — | 0 | — | [20260919T012239Z-replay-member-savings_balance-read-vision](runs/20260919T012239Z-replay-member-savings_balance-read-vision) |
| `vision-interstitial` | Pixels only: System Notice recognised and dismissed via the same app-profile handler | **success** | — | system_notice→dismiss | 0 | — | [20260919T012333Z-replay-member-savings_balance-read-vision](runs/20260919T012333Z-replay-member-savings_balance-read-vision) |
| `vision-obscured` | Pixels only: unknown popup over the target; the click lands on the overlay → caught at the right step because the screen did not change | **failure** | checkpoint_failed | — | 0 | — | [20260919T012516Z-replay-member-savings_balance-read-vision](runs/20260919T012516Z-replay-member-savings_balance-read-vision) |
