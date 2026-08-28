# chiap08 GLM admission launcher

This package contains an argument-free `skcapstone-glm-admission` executable and the
`skcapstone-glm-admission.service` oneshot unit. Both are disabled by default.
There is no timer and the unit has no `[Install]` section.

## Exact installation contract

A future separately authorized deployment must install one reviewed package
artifact into `/home/skuser01/.skenv`, copy the exact unit to
`/etc/systemd/system/skcapstone-glm-admission.service`, and pre-create these
local, non-Syncthing paths as `skuser01:skuser01` mode `0700`:

- `/var/lib/skcapstone-local/glm-admission`
- `/var/lib/skcapstone-local/glm-admission/transcripts`
- `/var/lib/skcapstone-local/glm-admission/rollback`

It must initialize `generation.json` as the reviewed v1 complete ledger owned by
`skuser01:skuser01` mode `0600`. The library creates only the fixed lock and
atomic ledger replacement beneath that directory. No command-line option,
environment variable, or configuration file can change authority, state,
CardStore, evidence, host, queue, executable, model, or worktree paths.

Enablement requires a separately authorized root-owned regular file:

```text
/etc/skcapstone/glm-admission-enabled  root:root  0600
SKCAPSTONE_GLM_ADMISSION_V1
```

Even with that file, execution must be an explicit `systemctl start`; nothing is
enabled or scheduled by installation. `ConditionHost=chiap08` and the executable
both check the physical hostname.

The consumer reads the fixed CardStore fold and accepts only exactly nine open,
unowned, dependency-free cards with a separate `verdict-evidence` link. That
link must bind a regular artifact by SHA-256, and the artifact must say exact
`PASS` for the same card. It claims all nine with supported coordination CLI
operations, reserves one generation with `admit_wave`, allocates exactly three
to each of chiap01, chiap02, and chiap03, then verifies every session. Any claim,
reservation, launch, receipt, HTTP 429, active hold, or queue failure stops the
wave, kills every exact session, releases every supported claim, and writes
rollback evidence. It never edits or clears the hold.

## Exact rollback

Before installation, retain SHA-256 hashes of the package and unit being
replaced. Rollback is:

1. Stop the oneshot if running. Do not delete transcripts or rollback evidence.
2. Remove `/etc/skcapstone/glm-admission-enabled` first, preserving a hashed copy.
3. Kill only session IDs listed in the preserved generation transaction.
4. Release only claims whose card, agent, and claim revision match that record,
   through `skcapstone coord release-claim`.
5. Restore the prior package and unit bytes by their recorded hashes, or remove
   both if the recorded before state was absent.
6. Run `systemctl daemon-reload`. Do not enable or start any unit.
7. Preserve the generation ledger, transcripts, rollback records, enable-file
   copy, package hashes, unit hashes, and command transcripts as evidence.
8. Do not clear or replace the fleet GLM dispatch hold.

This contract does not authorize installation, deployment, a provider canary,
gateway mutation, hold clearance, restart, or worker dispatch.
