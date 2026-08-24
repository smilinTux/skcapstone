# SKCP-00F3F completion dependency gate evidence

Card: `f701b0d3`
Date: 2026-08-23
Status: all acceptance checks passed; board completion follows this evidence link.

## Scope

This correction closes the final gate-ordering defect. Normal
`Board.complete_task` now reads the current folded dependency projection before
it writes legacy completion state or mirrors a CardStore completion event.

The check reads active and archived cards, so a completed archived dependency
remains satisfied. It treats unknown identifiers and every status other than
`done` as blocking. It preserves the existing idempotent return for a card
already recorded as completed. It does not alter the `force` claim override:
force may still claim a blocked card, but normal completion cannot bypass its
incomplete dependencies.

## Files changed

- `/home/skuser01/work/skcoord/src/skcoord/coordination.py`
- `/home/skuser01/work/skcoord/tests/test_claim_dependencies.py`
- `/home/skuser01/work/skcapstone/src/skcapstone/cli/coord.py`
- `/home/skuser01/work/skcapstone/tests/test_cli_coord_deps.py`
- this evidence record

## Completion-gate coverage

Focused tests prove that:

- an append-only dependency added after a card is claimed blocks completion;
- claimed, doing, and review cards all refuse completion around an open gate;
- unknown, open, and review dependencies are all listed in one refusal;
- a done archived dependency permits completion;
- rollback mode with `SKCOORD_CARD_STORE=0` still folds an append-only
  dependency and refuses completion;
- an already done card remains an idempotent re-completion no-op; and
- force-claim behavior remains covered without giving force semantics to
  completion.

The CLI now turns the same `ValueError` into a concise nonzero result with
the blocking identifiers rather than reporting a successful completion.

## Installed activation and live gate

Editable activation completed without a service restart:

```text
/home/skuser01/.skenv/bin/python -m pip install -e /home/skuser01/work/skcoord -e /home/skuser01/work/skcapstone
skcoord=/home/skuser01/work/skcoord/src/skcoord/__init__.py
skcapstone=/home/skuser01/work/skcapstone/src/skcapstone/__init__.py
```

The installed supported command appended exactly one attributed dependency:

```text
skcapstone coord add-dependency d0edbff1 --dependency bea13a70 --agent jarvis
Added dependency bea13a70 on d0edbff1.

event_id=c04a363468ed4efda9382fb497f195d6
writer=jarvis
node=chiap08
action=add_dependency
reason=Superseding exact-hash human approval must precede independent review completion.
count=1
```

The one live completion probe was expected to fail and made no successful
mutation:

```text
skcapstone coord complete d0edbff1 --agent jarvis
Error: Task d0edbff1 has incomplete dependencies: bea13a70
exit_code=1
```

At readback, `d0edbff1` remains `review` and `bea13a70` remains
`backlog`.

## Topology proof

Read-only installed Kanban verified that every 24-card catalog leaf and legacy
SKCP-01, SKCP-02, and SKCP-07 has a direct or transitive dependency path through
`d0edbff1` to `bea13a70`. The checked total was 27 cards. Examples include:

```text
SKCP-13 c6828b8a -> d0edbff1 -> bea13a70
SKCP-21A eddaa1fb -> d0edbff1 -> bea13a70
SKCP-30A 7888e091 -> d0edbff1 -> bea13a70
SKCP-40 008bd490 -> e6326000 -> d12b8951 -> d0edbff1 -> bea13a70
SKCP-07 f0c63c2a -> 94cbf19a -> d0edbff1 -> bea13a70
```

## Verification

```text
Focused SKCoord dependency, amendment, lifecycle, and mutation coverage:
77 passed in 2.25s

Relevant SKCapstone CLI and CardStore coverage:
47 passed in 1.32s

Full SKCoord suite under a 300-second bound:
413 passed in 3.81s

Ruff for changed SKCoord and SKCapstone files:
All checks passed!
```

An isolated installed parity and lifecycle fixture passed:

```text
coord parity --check
checked=2 matched=2 mismatches=0 missing=0
open: legacy=1 store=1 drift=0

coord reconcile-agents
clean=true
card_count=2
agent_count=1
```

Rollback-mode read-only verification passed:

```text
SKCOORD_CARD_STORE=0 skcapstone coord kanban --json
rollback_fold=d0edbff1:9442b3b3,bea13a70
bea13a70=backlog
```

The new code and evidence diff have no U+2013 or U+2014 characters.

## Rollback and non-authorizations

The append-only dependency may only be reversed by the supported attributed
`remove-dependency` operation under a separately authorized correction. The
source activation can be rolled back by reinstalling the previously qualified
package versions. The read-only rollback command above proves the human gate
remains folded without a legacy-board rewrite.

No human gate, independent review, implementation card, deployment, service
restart, commit, push, external action, Matter access, protected-data
retrieval, or HammerTime Inbox access occurred. Only `f701b0d3` will be
completed.
