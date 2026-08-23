# Fleet Services rollout (Phase 3)

## Pilot rollout, in order

1. Verify unit names on the target node BEFORE applying a spec:
   `systemctl --user list-units 'sk*'`. If a unit name differs from the
   pilot doc (for example the skchat daemon unit), fix the DOC, not the
   box.
2. Apply the pilot set on the control-plane node (.158):
   `for f in docs/fleet/pilot-services/*.json; do skfleet apply -f "$f"; done`
3. Run one controller pass and inspect:
   `skfleet reconcile && skfleet services && skfleet placements --kind service`
4. Watch one full sknoded cycle in report-only (default): statuses appear,
   ZERO actuation events. This is the safety soak.
5. Opt in actuation on .158 only: `skfleet actuation node-158 --enable`.
6. Acceptance drill (Card 3.1): `systemctl --user stop skwhisper@lumina`
   and confirm it is healed within 60s; set `"paused": true` in the doc,
   re-apply, stop again, confirm NO heal; unset paused. Kill-loop drill:
   break the unit (bad ExecStart), watch backoff events (10s, 20s, 40s),
   the CrashLooping condition, and exactly one sk-alert; repair the unit.
7. Freeze drill: `skfleet freeze --reason drill`, stop a pilot unit,
   confirm no heal and services stay up; `skfleet unfreeze`, confirm heal.
8. Wire the controller tick: add an skscheduler config job on .158 running
   `skfleet reconcile` every 60s (same jobs.yaml mechanism as existing
   jobs, notify: on_failure).
9. Enable actuation on node-41 after one clean day on .158. The local box
   stays report-only until explicitly decided otherwise (R4).

## Reversal

- One service: `"paused": true` + `skfleet apply -f <doc>`.
- One node: `skfleet actuation <node> --disable` (back to report-only).
- Fleet-wide: `skfleet freeze --reason <why>` (kill-switch; services keep
  running, all actuation halts everywhere).

## Onboarding wave 2 (Card 3.4)

1. Per service in `docs/fleet/services/`: verify the REAL unit name on the
   target node first (`systemctl --user list-units | grep -i <name>`); fix
   the doc if it differs, then `skfleet apply -f <doc>` and
   `skfleet reconcile`.
2. skmem-pg is EXCLUDED from fleet management (local-per-node by incident
   decision). Instead, declare the health probe on each node that runs it:
   add `"healthProbes": [{"name": "skmem-pg", "port": 5432, "condition":
   "SkmemPgReady"}]` to the node spec (via `skfleet apply` on a node doc).
   `SkmemPgReady=False` in `skfleet describe node <n>` is the alarm
   surface; nothing ever actuates skmem-pg.
3. Retire hand-run deploys: after one clean week, per-box `systemctl
   --user restart <unit>` habits are replaced by editing the Service doc
   and `skfleet apply` (heal is automatic); update any personal runbooks
   that mention direct systemctl for the onboarded set.
4. Acceptance (spec Card 3.4): `skfleet services` is a complete, truthful
   map of long-running fleet workloads; one full week with zero manual
   restart interventions on the onboarded set, or each intervention is
   carded as a bug.
5. R2 gate (spec Card 3.2 acceptance, checked here at full width): with
   all services onboarded, re-measure Syncthing item churn against the
   Phase 1 baseline; per-unit status files must be write-on-change quiet
   when the fleet is stable.
