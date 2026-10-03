# Collected-unit retirement source candidate

Card: `cd057f28`, producer: `codex-retirement-proof`.
Base: `b856ed2ca8b7bf2594f7c0387de259bbe3b23b15`.

The installed builder_retire.py, builder_terminal.py, builder_continue.py and
worker_git.py matched this base byte for byte before editing. Only
builder_retire.py changes in production source: node_check calls the existing
terminal.prove at both proof checkpoints. The import is local to avoid a
module initialization cycle. prove_dead remains the unchanged primitive used
by terminal.prove, so this does not recurse. Proof calls remain read-only.

The regression test failed before the fix with the same production process
death unavailable error on a collected unit. After the fix, this exact command
passed 100 tests in 3.16 seconds using Python 3.12.3 and pytest 9.1.1:

```text
PYTHONPATH=src /home/skuser01/.skenv/bin/python -m pytest tests/fleet/test_builder_retire_terminal.py tests/fleet/test_builder_retire.py tests/fleet/test_builder_terminal.py tests/fleet/test_builder_continue.py tests/fleet/test_builder_retry.py -q --disable-warnings
```

Black check passed for the changed Python files; git diff --check passed.
The new tests keep the actual retirement and terminal proof functions and
substitute system command observations. They exercise loaded and collected
success, live units, reused PIDs, mismatched loaded invocations and later
journal invocations. Rejections preserve request, status and workspace bytes
and create no evidence directory. Existing tests cover cgroup, journal,
permissions, qualified systemd version and receipt integrity denials.

Limitations: this is a source candidate, not installation or live retirement.
No data migration is required. Installation should replace only the changed
module after checking its original hash and preserving original bytes; do not
replace unrelated installed overlays. No changes were made to card 888b4895,
its request, status, claim or workspace. Independent gateway review is pending
and must bind the final source commit and tree before operational use.
