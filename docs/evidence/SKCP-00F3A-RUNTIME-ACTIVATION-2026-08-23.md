# SKCP-00F3A runtime activation evidence

Date: 2026-08-23

Card: `079cd760`

## Scope

This correction activates only dirty local SKCoord and SKCapstone development
sources in `/home/skuser01/.skenv`. No production deployment, package release,
service restart, commit, push, implementation source, or candidate artifact
changed.

## Claim release

Added `skcapstone coord release-claim TASK_ID --owner OWNER --agent ACTOR`.
It validates the exact owner and task, removes only that task from the owner's
legacy agent projection, leaves `completed_tasks` unchanged, and appends
CardStore `unassign` plus `move backlog` events with actor and released-owner
provenance. A second identical command is a no-op.

The only live release was:

```text
skcapstone coord release-claim d12b8951 --owner skcp-gate-probe --agent jarvis
```

Readback confirmed `d12b8951` is backlog/open, has no owner, and is not in the
probe agent's claimed or completed list. No other claim was released.

## Editable activation

Pre-state:

```text
skcoord 0.1.32 at /home/skuser01/.skenv/lib/python3.12/site-packages
skcapstone 0.15.49 at /home/skuser01/.skenv/lib/python3.12/site-packages
```

Activated command:

```text
/home/skuser01/.skenv/bin/python -m pip install -e /home/skuser01/work/skcoord -e /home/skuser01/work/skcapstone
```

Post-state:

```text
skcoord 0.1.33.dev0+g9133cfb.d20260823 editable from /home/skuser01/work/skcoord/src
skcapstone 0.15.50.dev0+gf1d688f.d20260823 editable from /home/skuser01/work/skcapstone/src
```

Installed `skcapstone coord release-claim --help` and
`skcapstone coord add-dependency --help` both succeeded.

Rollback is executable and requires no service restart:

```text
/home/skuser01/.skenv/bin/python -m pip install skcoord==0.1.32 skcapstone==0.15.49
```

Reactivation uses the editable command above. The rollback preserves all board
event logs and candidate artifacts.

## Live gate proof

The installed operator CLI added `079cd760` as an append-only dependency of
`90a02b0e`; F4 remains incomplete. Installed `coord kanban --json` reported
`d0edbff1` in each dependency list for `d12b8951`, `94cbf19a`, and `f0c63c2a`.

With `d0edbff1` incomplete, three normal installed CLI claims were expected to
fail and did fail with exit status 1. Each failure named `d0edbff1`. No claim
succeeded during this proof.

## Verification

```text
PYTHONPATH=/home/skuser01/work/skcapstone/src:/home/skuser01/work/skcoord/src \
  /home/skuser01/.skenv/bin/python -m pytest -q \
  /home/skuser01/work/skcoord/tests/test_claim_dependencies.py \
  /home/skuser01/work/skcoord/tests/test_dependency_amendments.py \
  /home/skuser01/work/skcapstone/tests/test_cli_coord_deps.py

16 passed in 0.63s
```

Ruff passed for every changed source and test file. The forbidden dash scan was
clean. Original candidate manifest-pinned files and unrelated repositories were
not modified.
