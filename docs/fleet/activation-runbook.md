# Software lifecycle activation runbook

**Scope:** SKCapstone, SKDashboard, and SKWorld only. Jarvis is Casey's
assistant and is not a lifecycle seat.

This is the shortest safe path to full role-specific capacity. Routine work is
automatic and notify-only. Casey is interrupted only when a governing catalog
or policy requires a human decision.

## 0. Local preflight

Run on chiap08 from the SKCapstone workspace:

```bash
python3 scripts/fleet/seat-manifest-audit.py --home "$HOME/.skcapstone"
python3 -m pytest -q tests/fleet/test_seat_boundaries.py tests/fleet/test_seat_runtime.py
python3 -m pytest -q tests/test_link_observation_feed.py tests/test_link_observation_producer.py tests/test_link_lineage.py
```

Required result: the manifest is healthy and all focused tests pass. A failed
preflight stops the affected seat and sends a notification. It does not wake
Casey or repair authority automatically.

## 1. GitHub control-plane readback

An administrator must read back branch protection, required checks, merge queue
behavior, deployment environments, environment protection, and concurrency for
these four control-plane repositories: `smilinTux/skcapstone`,
`smilinTux/skdashboard`, `smilinTux/skworld`, and `smilinTux/sk-standards`.
The readback belongs on card `05dc9297`. `sk-standards` is governance support;
the product lifecycle remains limited to SKCapstone, SKDashboard, and SKWorld.

The minimum control set is protected integration branches, required current
commit checks, reviewed merge paths, isolated deployment environments, one
deployment per environment, and immutable release evidence. This follows the
GitHub branch/deployment controls, DORA trunk-based development, and OpenSSF
Scorecard supply-chain guidance listed in
`docs/fleet/software-lifecycle-practices.md`.

No agent changes these settings. Current API responses are insufficient to
prove their presence because the active token lacks confirmed administrator
readback.

## 2. Finish Link lineage before enabling Link

Refresh the paginated open-PR inventory and dry-run reconciliation:

```bash
PYTHONPATH=src python3 scripts/fleet/link-lineage.py \
  --home "$HOME/.skcapstone" \
  --output "$HOME/.skcapstone/coordination/link-lineage.json"
```

Acceptance is `unresolved: 0`, with every included PR bound to exactly one
source card, one independent review card, current card revisions, and the
observed PR head and base SHA. Stale or unmanaged work may be excluded only
with an explicit bounded record containing the PR number, observed head/base,
reason, owner, and expiry. Never invent card mappings to improve coverage.

Then produce the mediated feed in dry-run mode:

```bash
PYTHONPATH=src python3 -m skcapstone.link_observation_producer \
  --repo smilinTux/skcapstone \
  --lineage "$HOME/.skcapstone/coordination/link-lineage.json" \
  --output "$HOME/.skcapstone/coordination/link-observations.json" \
  --dry-run
```

Require a healthy feed with record count equal to the open-PR inventory. Only
then may the Link timer be enabled. Link still has no GitHub credentials and
consumes only this mediated feed.

## 3. Niobe bounded live transition

Niobe continues shadow beats automatically. Casey approval is required only
to change authority from `shadow_only` to live mutation. The approval record
must state:

- host: `chiap08`;
- allowed actions: claim, release, launch, stop, and reassignment;
- excluded actions: merge, deployment, application actuation, and external
  dispatch;
- exact card families and products in scope;
- expiry time and rollback owner; and
- rollback: disable the live unit, restore `shadow_only`, and preserve all
  receipts.

After that exact record exists, Niobe must re-read the card, claim revision,
capability, and process state before each mutation. Duplicate, stale, replayed,
or contradictory recommendations fail closed. No extra human gate is added to
normal card execution.

Casey approved card `c4e7a9b2` on 2026-09-06. The machine-readable decision is
`scripts/fleet/niobe-activation.example.json`: decision
`casey-c4e7a9b2-20260906`, host `chiap08`, live unit
`skfleet-niobe-live.timer`, rollback owner Casey, and expiry
`2026-10-06T22:00:00+00:00`. The validator requires the exact three-product
scope, the five fleet actions only, explicit denial of merge, deploy,
application actuation, and external dispatch, the immutable card core hash,
the named unit, exact rollback action, and a future expiry.

## 4. Tank and Seraph

Tank is active as a card-scoped worker, not a permanent daemon. Tank executes
only an exact approved release or deployment card with pinned artifact and
rollback evidence.

Seraph uses the bounded `skfleet-seraph.timer` on the active control-plane
host. Each invocation may claim and launch at most one canonical review card,
and the launched reviewer must be independent of the candidate producer.
Seraph independently verifies candidate, release, deployment, health,
rollback, and rerun behavior. Ordinary review work is automatic and
notify-only; production authority remains an external gate.

## 5. ATLAS and Jarvis

ATLAS is active for bounded presence and exact card-scoped operations. Its
presence cycle reads SKMail and emits health but does not claim work or actuate.
An operations card reaches ATLAS through Niobe and remains subject to the
ActionIntent catalog, exact capability, rollback, and verification gates.
Routine catalog-authorized work is notify-only.

Jarvis remains Casey's assistant. Jarvis may use emergency card, fleet, merge,
deployment, release, verification, and actuation tools when Casey directs it,
but does not claim recurring lifecycle ownership or enter lifecycle timers.
The runtime exposes Jarvis emergency operations only through
`JarvisEmergencyGateway`. Each call verifies an unexpired Casey signature and
exact action, target, change, and `skcapstone,skdashboard,skworld` scope before
calling the mutation. A missing or mismatched direction fails closed.

## Rollback

Disable only the affected unit, preserve evidence, and return the seat to its
last safe state. The approved Niobe rollback is:

```bash
systemctl --user disable --now skfleet-link.timer
systemctl --user disable --now skfleet-niobe-live.timer
systemctl --user enable --now skfleet-niobe-shadow.timer
```

The initial review `c4e7a9b3` failed closed on a stale-card fence. Rereview
`c4e7a9b4` passed that repair, and exact target-parity review `c4e7a9b6`
passed the final candidate. The 2026-09-06 cutover disabled the legacy
`skfleet-rotate.timer` before enabling `skfleet-niobe-live.timer`, so there is
only one live dispatcher. The read-only shadow timer remains enabled. Tank
rolls back through its card-pinned artifact procedure. Seraph rolls back by
disabling `skfleet-seraph.timer`, preserving append-only review evidence, and
reverting its pinned source commit. A feed failure disables Link's eligibility
input; it does not trigger GitHub mutations.
### Converge the six lifecycle profiles

After independently reviewed package installation and before enabling timers,
converge the packaged control record and role profiles into the existing
sovereign agent homes. The command refuses missing or mismatched identities and
captures every replaced file in an exact rollback bundle.

```bash
python -m skcapstone.lifecycle_seats converge \
  --home "$HOME/.skcapstone" \
  --rollback-dir "$HOME/.skcapstone/rollback/lifecycle-six-seat-<change-id>"
```

Rollback fails closed if any installed target changed after convergence:

```bash
python -m skcapstone.lifecycle_seats rollback \
  --home "$HOME/.skcapstone" \
  --rollback-dir "$HOME/.skcapstone/rollback/lifecycle-six-seat-<change-id>"
```
