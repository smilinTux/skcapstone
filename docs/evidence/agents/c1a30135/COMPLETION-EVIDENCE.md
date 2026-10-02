# c1a30135 source inspection transport evidence

Status: source candidate verified; independent review, installation, runtime
calibration and production acceptance remain with root. Owner
`codex-inspection-transport-c1a30135`, claim
`2954c81bbf4a4a188c24d7b345bbc804`. Exact base
`df7f99daa0c635be7192c4cbbf45050f205df08e`; dependency `c1a30124` was DONE.
Parent `c1a30126` is not completed by this repair.

## Cause and changed files

The retained review is valid, but the authority's PrivateTmp user namespace
enters AppArmor `unprivileged_userns`, which denies capabilities and stacks
that denial onto child executables. A bwrap attachment cannot undo the parent
denial. The same proposal inspection succeeds outside that inherited profile.

- `src/skcapstone/fleet/source_bundle.py`: only `_inspect` transport changes,
  plus the unique-unit identifier import. The complete bwrap argv is unchanged.
  Native `systemd-run --user --wait --pipe` starts that command with explicit
  NoNewPrivileges, runtime/resource bounds and no environment expansion.
  Output uses a private temporary file, a service file-size limit and a bounded
  read. The exact unique unit is stopped even on launcher failure or cancellation.
- Existing source and review evidence tests cover the transport and hardened
  boundary. Task plan, production runbook, changelog and this evidence record
  the repair and remaining acceptance gates.

No broker, daemon, fallback, new dependency, global AppArmor/sysctl change,
authority isolation change or scheduler change is included.

## Observed verification

Python 3.12.3, isolated worktree:

```text
PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest tests/fleet/test_source_bundle.py tests/fleet/test_production_review_evidence.py tests/fleet/test_canonical_review_opener.py tests/fleet/test_production_custody.py tests/fleet/test_builder_retry.py tests/fleet/test_builder_continue.py -q --tb=short
131 passed in 22.04s; no failures, errors or skips.
```

The focused composition covers real source bundle export/import, current native
canonical opener and acceptance joins, retained source custody, builder retry
and continuation. The new tests verify literal argv preservation, unavailable
manager refusal, malformed/nonzero/oversized output refusal, denied source
write/home/network access, private tmpfs, zero effective capabilities and exact
unit cleanup after a lost launcher with forked descendants.

Private `SERVICE-BOUNDARY.json` records a real transient authority-equivalent
unit with PrivateTmp and NoNewPrivileges. Its observed profile was
`unprivileged_userns (enforce)`. The repaired inspection validated unchanged
review commit `617ce0b51944abb535f8f14be129207ca8e6a2a3`, bound to source
`393ce1765245f3bcf4d51f9fddeb4a9ccae2ddd4`, tree
`4d53e807d22744a34255e7923e217396d1527fce`, verdict PASS. No model was called.

The child retained `skfleet_bwrap//&skfleet_unpriv_bwrap (enforce)`,
NoNewPrivileges and zero effective capabilities. Home was absent, source writes
failed read-only, networking failed with ENETUNREACH and private tmpfs was
verified. Malformed, failed and oversized output were refused. A real 40-second
runtime expiry with forked descendants failed closed in 40.138 seconds.
All six exact inspector units were not-found/inactive and their cgroups absent.

Ruff, Black and diff checks are recorded in the private final-check receipt.
The source and review checkouts and native claims were retained unchanged.

## Installation boundary and rollback

The sole runtime delta is `fleet/source_bundle.py`. Private preimage and
postimage bytes are verified against installed and candidate source. The
installed preimage SHA-256 is
`934416d3fc40a1811b25f830eb61e8de1344af307f2d527d84efb2c1ec428643`;
the candidate postimage is
`2cf9370dc66390d00d3865f50a0635d1a2a0d6d701aeaf629b8f2087751e244c`.

The runtime fingerprint changes from
`bf41802c460db157f3db56039ca33b1f8cf78fa9f13d81f978133e34089b683c` to projected
`6b90fc7ed518ea08941f4287ecbc0870b2eaa671ec2f2a06ff3d46ac78d4e8c7`.
The old test profile must not authorize the new runtime. Root must independently
review exact source, install only this backed-up delta during its quiet window,
replay the installed hardened boundary, and recalibrate through the existing
native test-profile flow. Preserve the valid committed review and original
source rather than asking a model to repeat it.

Rollback restores the exact module and matching profile backup, verifies their
hashes and the prior runtime fingerprint, and preserves all candidate, test,
review and claim evidence. No data migration is involved. No installation,
profile write, timer change, claim release, push or production completion was
performed by this worker. Runtime calibration and actual acceptance remain
unproven until root supplies their receipts.

Private evidence, descriptor and verified bundle are under
`~/.skcapstone/evidence/work/c1a30135/`, with owner-only 0700 directory and
0600 regular files. Native card `c1a30135` links the exact source descriptor.
