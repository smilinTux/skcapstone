# Provenance correction, 2026-09-16

Two commits on `spec/nimble-factory-2026-09-16` carry incorrect authorship and one
carries a message that does not describe its contents. This file states the facts
rather than rewriting history, so the error stays auditable.

## What is wrong

| Commit | Claims | Actually contains |
|---|---|---|
| `fdd2bbe0` | authored by Mero, "docs(spec): Amendment A" | 182 lines of spec by Mero, PLUS 129 lines of `skfleet_readiness.py` and `tests/test_skfleet_readiness.py` written by the Task 0 implementer (session identity Lumina) |
| `3279797b` | authored by Mero, "chore(sdd): ledger through Task 1" | zero ledger content. 38 lines of the Task 0 implementer's effective-environment fix |

## True authorship

- `scripts/fleet/skfleet_readiness.py` and `tests/test_skfleet_readiness.py`:
  written by the Task 0 implementer across `a7dde8d4` (correctly attributed) and
  the two commits above (incorrectly attributed). The design of
  `parse_systemd_environment`, `systemd_effective_environment`, and the decision
  to issue separate `systemctl show` calls per property are all its work.
- `docs/superpowers/specs/2026-09-16-nimble-factory-design.md` Amendment A:
  written by Mero.

## Cause

Mero ran `git add -A` in a worktree while a subagent was actively editing files in
it. The staging swept up the agent's in-progress work and committed it under
Mero's identity.

This is the same defect Amendment A.8 of the spec describes: concurrent edits in a
shared checkout, where the loss or corruption is silent. It was committed against
a subagent rather than a fleet host, which shows the rule is not specific to
hosts.

## Correction to practice

While any subagent is working in a worktree, the controller does not stage from
that worktree. Either stage explicit paths it owns, or wait for the agent to
report. `git add -A` in a shared working tree is the mechanism that caused this
and is not used again in that condition.

No history was rewritten. The commits stand; this file is the correction.
