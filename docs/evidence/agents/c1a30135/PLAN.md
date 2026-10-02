# Hardened service inspection transport

Card `c1a30135`, owner `codex-inspection-transport-c1a30135`, claim
`2954c81bbf4a4a188c24d7b345bbc804`. Base
`df7f99daa0c635be7192c4cbbf45050f205df08e`. Dependency `c1a30124` is DONE;
parent `c1a30126` remains in its production trial.

The approved bounded repair changes only the existing `_inspect` transport.
The user manager starts the identical bwrap command before a caller's
PrivateTmp user namespace can stack the generic AppArmor capability denial.
The coordinator keeps PrivateTmp and NoNewPrivileges. Inspection keeps
NoNewPrivileges, bwrap private tmpfs, no home or network, and read-only source.

1. Wrap the same argv in the existing native transient user-service mechanism;
   bound runtime, resources and output, and stop the exact unique unit on exit.
2. Exercise source/review fixtures, denied access, malformed/oversized output,
   lost launcher and whole-cgroup cleanup. Preserve argument bytes.
3. Under the exact hardened boundary, inspect retained review commit `617ce0b`
   without rewriting it or running another model review. Probe real runtime
   expiry with descendants, then verify each unit and cgroup is absent.
4. Run focused integration and quality checks, commit exact source, and prepare
   a verified private bundle, backup bytes and runtime-profile impact.
5. Hand independent review, backed-up installation, post-install boundary
   replay and fresh runtime calibration to root. No global AppArmor/sysctl,
   service isolation, scheduler, source custody or claim changes.
