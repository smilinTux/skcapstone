# Fleet control-plane cold start (Phase 1)

From a single bare box to a managed fleet, no hand-authored fleet files.
Spec: skos/docs/specs/2026-07-27-skworld-fleet-control-plane-design.md,
section 9.

## First box (control plane, normally .158)

1. Install skcapstone into ~/.skenv (scripts/install.sh). Syncthing shares
   ~/.skcapstone as usual; the fleet tree lives at ~/.skcapstone/fleet/.
   Add `.events.lock` to the share's .stignore (event log lock sidecars).
2. Install and start the node agent:
   cp systemd/sknoded.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now sknoded
3. sknoded self-reports and writes a join request (status/<self>/join.json).
4. Admit yourself (first-node special case):
   skfleet admit --bootstrap --preset node-158
5. Verify: skfleet nodes shows node-158 Ready with labels
   always-on, dev-primary, control-plane.

## Every additional box (.41, .100, local)

1. Install skcapstone; let Syncthing sync ~/.skcapstone.
2. Start sknoded (same unit as above). The box appears in skfleet nodes as
   Pending within one sync interval.
3. From any operator seat: skfleet admit --preset node-41   (or node-100,
   node-local). Known-key auto-admit is available for rebuilds of trusted
   boxes via admission.auto_admit.
4. Verify: skfleet nodes shows the node Ready with the preset labels;
   node-100 must report gpu and vram_gb.

Note: the local box stays report-only in Phase 1 (no actuation exists yet
anywhere; sknoded only self-reports).

## Travel taint on .41

When .41 leaves the LAN (tailscale-only), record it on the node object:
skfleet describe node node-41 to view, then re-admit is NOT needed; edit
via cordon for full exclusion, or (Phase 2+) set the taint
travel=true:PreferNoSchedule with skfleet apply. Until preference scoring
lands (Card 2.1b), the taint is advisory and cordon is the operative tool.

## Kill-switch

skfleet freeze --reason "why"    halts all actuation fleet-wide
skfleet unfreeze                 resumes
Self-report and running services are never affected by freeze.

## Order of operations (why it works)

skcapstone daemon + Syncthing, then sknoded self-report, then admission,
then NodeController ticks, then (Phase 2) the scheduler, then (Phase 3+)
controllers become self-hosted as fleet objects themselves.
