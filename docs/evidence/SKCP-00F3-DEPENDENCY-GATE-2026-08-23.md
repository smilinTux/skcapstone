# SKCP-00F3 dependency gate evidence

Date: 2026-08-23

Card: `83a404bf`

## Scope and preservation

This remediation adds append-only dependency amendments to the SKCoord card
fold and exposes them through the SKCapstone coordination CLI. It does not
complete, claim, deploy, commit, or push any implementation card.

The original candidate manifest remained unchanged. Its SHA-256 was
`88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`.
Every artifact listed by
`docs/review/SKCP-00-CANDIDATE-MANIFEST.json` passed `sha256sum --check`.

## Implementation

SKCoord now folds `add_dependency` and `remove_dependency` events from each
card's per-writer event log. `add_dependency` requires known, distinct cards
and a reason. It is idempotent: a retry that finds the effective dependency
already present appends no duplicate event. `remove_dependency` provides an
append-only, attributed rollback. Neither operation rewrites task JSON or
CardStore `core.json` birth facts.

SKCapstone provides the supported commands:

```text
skcapstone coord add-dependency TASK_ID --dependency GATE_ID --reason TEXT --agent AGENT
skcapstone coord remove-dependency TASK_ID --dependency GATE_ID --reason TEXT --agent AGENT
```

The legacy-read rollback projection also folds dependency amendments. Therefore
setting `SKCOORD_CARD_STORE=0` cannot reopen a dependency gate that remains in
the event log.

## Live board migration

The source-built supported command appended one attributed event to each
existing downstream card. Original identifiers, creators, creation times, and
task files remain unchanged.

| Downstream card | Added dependency | Writer | Result |
| --- | --- | --- | --- |
| `d12b8951` | `d0edbff1` | `jarvis` | Present exactly once |
| `94cbf19a` | `d0edbff1` | `jarvis` | Present exactly once |
| `f0c63c2a` | `d0edbff1` | `jarvis` | Present exactly once |

At verification time `d0edbff1` was in `review`, not `done`. Normal claims
were refused without `--force`:

```text
d12b8951: incomplete dependencies: d0edbff1
94cbf19a: incomplete dependencies: d0edbff1
f0c63c2a: incomplete dependencies: 94cbf19a, d0edbff1
```

No target card was claimed or completed during this verification.

## Synthetic gate and rollback evidence

Focused tests create a synthetic review gate, prove a normal claim is refused,
complete only that synthetic gate, and then prove a normal claim succeeds.
They also prove an append-only remove event restores eligibility and that the
legacy rollback projection still enforces an added dependency before removal.

Exact verification:

```text
PYTHONPATH=/home/skuser01/work/skcapstone/src:/home/skuser01/work/skcoord/src \
  /home/skuser01/.skenv/bin/python -m pytest -q \
  /home/skuser01/work/skcoord/tests/test_dependency_amendments.py \
  /home/skuser01/work/skcoord/tests/test_claim_dependencies.py \
  /home/skuser01/work/skcapstone/tests/test_cli_coord_deps.py \
  /home/skuser01/work/skcapstone/tests/test_coord_amend.py

27 passed in 0.74s
```

Ruff over all changed source and test files reported `All checks passed!`.

## Canonical SKCP-04 reconciliation

Before changing `8b0ad975`, the card projection showed `d79100a7=done`.
After that verification, the append-only label removal command removed
`canonical-pending-d79100a7` from `8b0ad975`. The current projection confirms
the stale label is absent.

## Operational rollback

To reverse a dependency amendment, append a removal event with the same card
and gate IDs and a new audit reason. Do not edit task JSON or `core.json`:

```text
skcapstone coord remove-dependency d12b8951 --dependency d0edbff1 \
  --reason "Approved rollback reason" --agent <agent>
```

The synthetic rollback test proves this behavior. No live rollback was run,
because the required gate remains active.

## Limitations

The implementation is present in the authoritative local SKCoord and
SKCapstone source trees and was exercised through `PYTHONPATH`; no package
release, installation, deployment, commit, or push was performed. Other hosts
need the same released source before their local CLI binary exposes the new
verbs, while the appended live events remain audit records.
