# Collected unit delta over reviewed f7eb372

Scoped card link: `collected_unit_terminal_scope`, event
`84c9f66fc1ca4e6caab532f179f64956`. Original fleet and coordination independent
reviews remain inherited evidence. This delta needs its own review before use.

The native dispatcher attempts to persist the first exact loaded terminal
snapshot, binding card, request, owner, claim, attempt, unit, invocation, process
birth token and boot. Failure to observe or persist it does not broaden existing
exit handling. Collected-unit continuation remains unavailable without proof.

For a collected unit only, native continuation rechecks current absent dispatcher
PID and exact cgroup. Trusted manager journal entries must have the current boot,
UID, systemd executable, manager init.scope, exact unit and valid invocation.
There must be one matching start after dispatcher birth and no subsequent
invocation. Existing native receipts supply terminal evidence without requiring
a resource message. Reconstruction additionally requires one final resource
message from `unit_log_resources`, exact installed systemd 255.4-1ubuntu8.17, and
the source qualification recorded on the card. The full entry, journal cursor,
canonical entry hash and qualification are preserved immutably with mode0600
inside an owned mode0700 directory when apply is explicitly requested.

The source qualification establishes one call to unit_log_resources, inside the
nonterminal to inactive/failed transition after cgroup pruning in exact Ubuntu
unit.c. No distro patch touches this file. Source SHA256 values are pinned in
the helper and card. This establishes termination only, never success.

Validation from the isolated source path:

```text
PYTHONPATH=src python -m pytest -q tests/fleet/test_builder_terminal.py tests/fleet/test_builder_continue.py tests/fleet/test_builder_dispatch.py tests/fleet/test_production_builder.py tests/fleet/test_builder_retire.py tests/fleet/test_builder_retry.py tests/fleet/test_production_exit.py
208 passed in 9.26s, zero failures or skips
```

The 32 new terminal tests include spoofed sender, stale boot and process birth,
unit/invocation mismatch, later invocation, live/unknown state, ambiguous or
oversized journal, changed/redirected/private receipt, denied PID access,
existing cgroup, future timestamp, dispatcher receipt persistence, and
continuation refusal before source preservation. Existing loaded-unit
prove_dead behavior is unchanged.

Actual chiap02 qualification used a private copied package. The read-only helper
passed for a8300e02 invocation `3cae1708341b44d8934f3c9f032e3fb9`, current boot
`8627ee284c0a4ce9891d005ec9ac16aa`, exactly two manager entries, terminal entry
SHA256 `54d8b673d303eed69695ff8bdbbd7a8d9dd88275f455b80261b0b983d0f9eb9e`.
No terminal receipt, grant, status, claim or live runtime was written. No worker
was restarted. This does not substitute for root's later fresh native check.

Limitations: journal history must remain available, bounded to64 entries for the
exact unit, with its unique start. Missing/vacuumed history, a new boot, reused
PID or unqualified reconstruction version fail closed. A durable native receipt
does not require the resource event or current reconstruction package version.
Existing consumed-before-launch custody remains fail closed without automatic
replay. Installation requires managed daemon reload and profile requalification.

Rollback restores exact module preimages. Preserve all immutable termination
receipts, continuation sidecars, source archives and claims.
