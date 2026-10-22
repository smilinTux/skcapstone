# Recover Unobserved Worker Terminal Reservations Implementation Plan

**Goal:** Free a strict worker quota only when exact journal evidence proves the fenced systemd invocation started and exited.

**Architecture:** Extend the existing production admission recovery path for a started worker whose `observed.json` write was missed. Validate the immutable intent and start fence, correlate one invocation's start and terminal journal events, write the normal observation receipt, then let existing terminal reconciliation write its journal receipt. Preserve charges whenever evidence is absent or ambiguous.

**Tech Stack:** Python 3.11/3.12, pytest, systemd journal JSON.

**Spec:** Current chiap08 incident: `070a85f6`, unit `skfleet-worker-codex-070a85f6.service`, reservation `87530478...`; intent/start/fenced-start exist, observed is absent, exact invocation `aa28f2f344734312a3df85e3a60d4493` has Started and Consumed journal records.

## Constraints

- Branch from `origin/main`; code reaches hosts only through merged main and the rollout gate.
- Never edit or clear live admission files, another owner's claim, or systemd state by hand.
- Require exact host, unit, reservation, claim binding, start fence, invocation, and terminal event; refuse incomplete or ambiguous journals.

## Tasks

1. Add regression tests in `tests/fleet/test_unobserved_builder_terminal.py` for one exact unobserved generic worker start plus terminal event recovering its observation, and for ambiguous/mismatched invocation histories retaining the reservation.
2. Run those tests and confirm they fail against current main.
3. Add the smallest generic recovery helper in `src/skcapstone/fleet/production_admission.py`, invoked beside builder recovery; reuse existing journal validation and write-once receipt APIs.
4. Run the focused tests, the full related admission test set, format/lint, then commit the fix and required changelog entry.
5. Push the branch, open one PR with the exact incident evidence and fail-closed behavior, and enable rebase auto-merge.
