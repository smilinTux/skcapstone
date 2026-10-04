# Failed remote review retirement evidence

Card: `30104871`. Production incident: `c60a542e`.

The remote review request remains held after a Node 18 `globSync` startup
failure, despite exact claim release. The current node-owned status still says
`running` and has no retained review packet. The managed unit on chiap03 is
failed with invocation `f146a21bc90545b4a33f28aca0fa6c2e`, exit status 1,
MainPID 0 and ControlPID 0.

The candidate operator tool qualified the real request without applying any
retirement. It ran from this worktree with `PYTHONPATH=src` and the dispatcher's
`SKFLEET_PRODUCTION_POLICY` and `SKFLEET_AUTHORITY_HOST=chiap08` context.
The exact proof is retained at
`reviews/jarvis-supply-20261004/c60-retirement-check.json` under the operator home.

Pinned request file SHA256:
`58140a9786f64171888f225b00d4279e0b559b2afd5013a76cc1b4797d636442`.
Pinned status file SHA256:
`655a2e813035dbbb517fc221d9aec98fe67af10c6f5ff3fc2de3321f3c702fca`.
Review state revision:
`eb9b9ffff9d5d4e9baa993ab2b71adb209fbe0152b1da224031a7f726314816c`.

Validation command:

```sh
python -m pytest -q tests/fleet/test_review_retire.py \
  tests/fleet/test_remote_review_dispatch.py \
  tests/fleet/test_remote_review_execution.py \
  tests/fleet/test_builder_active_hold.py -m 'not host_systemd'
```

Result: 52 passed, 35 deselected. Black, Ruff and `git diff --check` pass.
The checks cover missing release proof, changed hashes, successful exits,
unproven process death, immutable archives, exact replay and interrupted
pointer cleanup. This is not an executed production retirement.

Recovery is append-only in the card history and retains the workspace and
failed request/status/exit bytes. A new review offer still follows current
source, policy, provider and custody gates. Rollout is from merged main;
no candidate was installed into a production interpreter.
