# ZIOWK01 legacy admission fence

Card f1be0929, jarvis, 2026-09-29 21:46 UTC.

## Applied and verified

Used the existing native per-node report-only setting, not a new daemon,
patched package, stopped service, or global fleet freeze:

```sh
skcapstone fleet actuation node-ziowk01 --disable
```

The prior node spec is recorded in ZIOWK01-BEFORE-REPORT-ONLY.json. The only
operational field changed is spec.actuate, true to false. Role, labels,
address, cordon, and taints are preserved. Generation advanced from 8 to 9.

Before the change, the target's native local_services query returned no
fleet-managed service placements. Its builder records had no running job,
the four current queued offers were expired, and the live fleet collector
found no worker process on ZIOWK01. No existing job, claim, or artifact was
removed. Existing application processes were not stopped or restarted.

The installed consumer checks spec.actuate before reading jobs or claims.
Report-only also disables future service convergence on this node, but there
are currently no fleet-managed service placements there. Self-reporting is
independent and continues. Do not describe this as disabling only a timer.

The same live node process was observed before and after:

- sknoded.service active, MainPID 1063042.
- InvocationID 1f4b4576483e4c5f8772c7e4285c0abd.
- Target setting updated 21:45:13 UTC.
- Subsequent heartbeat 21:46:28 UTC, published 21:46:29 UTC.

## Tests and rollback

Two scratch-fleet tests passed locally and on ZIOWK01 (0.084 s and 0.026 s):

1. Native set_actuation preserves every other spec field and label, and
   enabling restores the original spec and labels.
2. The installed consumer in report-only mode does not read dispatch statuses,
   invoke the actuation gate, launch jobs, or touch claims.

The full local dispatcher suite now has 41 passing tests in 1.001 s.
The preceding 39-test suite passed on chiap04; the two new legacy gate tests
ran on the actual ZIOWK01 installed library. Do not conflate these scopes.

Rollback uses the native fleet CLI with --enable after confirming the node
still has this exact held spec and that no competing writer changed it.
Tested in a scratch fleet, not by briefly re-enabling live admission. Doing
so restores legacy autonomous admission and is not safe alongside an enabled
central dispatcher unless those paths have first been reconciled.

## Control-plane delivery finding

The initial chiap08 CLI write produced generation 9, actuate=false, but
ZIOWK01 retained generation 8. Syncthing was active on both hosts. ZIOWK01
connects through its configured peer, not directly to chiap08. The decisive
evidence was chiap08's own Syncthing database still indexing the old node
file modification time (17:23:45 UTC), despite the new file on disk.
The local database status showed sync-preparing with no watchError and no
pullErrors. A targeted rescan observation timed out; that does not prove
the server-side scan stopped or failed. No destructive sync override,
replica replacement, or sync-service restart was used.

Because delivery was not established, the same desired setting was applied
through ZIOWK01's native fleet CLI under jarvis identity. Both node replicas
now have generation 9 and actuate=false. Their operator timestamps/host
attribution differ. This is not proof that the sync transport is repaired.

Before live worker admission, claim authority must be revisited: remote
workers currently claim through their local coordination CLI. With delayed
replication, local readback alone cannot prove agreement with the dispatcher
host's current claim. Keep admission held until authority-side claim binding
and remote visibility/refusal behavior are reviewed and tested.

## Existing crew coordination

Non-interrupting FYI notices were sent using coord mail to 12 verified current
owners of the named GLM and DeepSeek cards. The notice asks them to continue
their current exact claimed task, preserve normal evidence/claim discipline,
and not self-refill or spawn replacements afterward until central admission
is released. It expressly does not request abandonment, immediate reply,
deployment, push, protected-data access, or expanded actions.

Recipients: pi-ds2-02963e5f, pi-ds2-04e788cb, pi-glm-060e165c-r2,
pi-ds2-1960b104, pi-ds2-1a5cd985, pi-ds2-236377aa, pi-glm-a8200001-r2,
pi-glm-daa1cd5f-r2, pi-glm-f7f6daeb-r2, pi-glm-13580287-r2,
pi-ds2-256586ab, pi-ds2-2509445c. Mail submission succeeded for all 12.
No acknowledgement or compliance is claimed. No terminal prompt was injected.

The new dispatcher remains held and its timer disabled. Independent review,
safe cross-host claim and artifact handoff, route qualification, distributed
canary, external verification, and useful-work metrics remain incomplete.
