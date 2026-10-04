# 9aa11902 builder test environment

The chiap04 native producer committed `b58fde04431b99cca687a25bea26b02053a7e5f4`
and recorded a bound `PASS_FOR_REVIEW` with shared evidence SHA256
`47c8df2e250636cc224095c2df5999c82eb4723a90a1eeed0887abe79a2c2aff`.
The controller later published its exact source bundle and left the claim in
`awaiting-review`. This is a producer handoff, not a successful independent
review or an accepted code change.

The producer's full `tests/fleet/test_source_bundle.py` run was **12 passed,
12 failed**. The 12 failures required a working host user systemd manager;
the worker's clean `env -i` launch omitted `XDG_RUNTIME_DIR` and
`DBUS_SESSION_BUS_ADDRESS`, and `systemctl --user` reported `Failed to connect
to bus: No medium found`. Its new pack-thread regression passed. These are
environment failures, not evidence that the candidate's source behavior
regressed. They also are not a passing full source-bundle suite.

On current main before this change, reproducing with the same clean
environment gave **11 passed, 12 failed**; the candidate adds one passing
regression. The qualified test executor uses a separate bwrap sandbox with
`--clearenv` and no `/run/user` mount, so passing bus variables to the Pi
builder would not make host-systemd tests executable during qualification.
The six affected test functions, totaling 12 cases, now carry
`host_systemd`. Qualified producer recipes deselect that marker and still
require their configured number of passing tests from every selected file;
hosted CI continues to run the marked cases on its disposable user manager.

Validation of this change on main: with `env -i`,
`pytest -q -m 'not host_systemd' tests/fleet/test_source_bundle.py` gave
**11 passed, 12 deselected**. With the host user manager available,
`pytest -q tests/fleet/test_source_bundle.py` gave **23 passed**.

The original completion evidence remains unchanged at
`~/.skcapstone/evidence/work/9aa11902/completion-b58fde04431b99cca687a25bea26b02053a7e5f4.0625a6510fc9452097ebf0db7c44f27b.md`.
