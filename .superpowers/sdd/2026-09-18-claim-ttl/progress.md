# SDD ledger — plan: docs/superpowers/plans/2026-09-18-claim-ttl.md

Spec: docs/superpowers/specs/2026-09-18-claim-ttl-design.md
Branch: feat/claim-ttl-heartbeat (worktree ~/skworld-worktrees/claim-ttl-skcapstone)
Base: e432e227 (origin/main, the deployed commit)

## Pre-flight conflict scan

| rows | finding |
|---|---|
| T1 vs T2 | Same file (claim_expiry.py). T2 appends `observe` + `import json`; T1 defines the dataclasses it returns. Sequential, dispatched to ONE agent to avoid two review cycles on one file. No conflict. |
| T2 vs T3 | T3 consumes observe/evaluate/ttl_seconds_from_env/mode_from_env. Signatures fixed in T1/T2 Interfaces blocks. Agree. |
| T3 vs T5 | T5 adds a console script pointing at claim_expiry_cli, created in T3. T5 MUST follow T3 or packaging names a nonexistent module. Ordered. |
| T4 vs all | Only task touching skfleet-rotate.py. Consumes T1-T3. Last. |
| T1 self | Tests assert boundary is exclusive (`idle <= ttl` is not reclaimable) and impl uses `idle <= ttl_seconds`. Agree. |
| T2 self | Tests assert re-claim supersedes (latest generation wins) and impl replays in seq order setting revision on each claim. Agree. |
| T3 self | Test asserts exit 0 with findings; impl returns 0 unconditionally. Agree. |
| T4 self | Plan gives structure not full code, by design: the helper must bind to names (HOME, log, d, HOST) that exist in a 6842-line script the plan cannot quote safely. Step 1 mandates reading them first. |

Ruling: T1+T2 batched to one implementer (same file, tightly coupled).
Cost if wrong: one larger review surface instead of two small ones.

## Tasks
