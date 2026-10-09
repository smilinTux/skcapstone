# f1be0929 resource rollout result

2026-09-29, jarvis. This is partial deployment evidence, not final acceptance.

## chiap02 readiness repaired

Before: /swap.img size 8589930496 bytes, used 8589705216, priority -1.
MemAvailable 11993928 kB; SwapFree 220 kB. Existing SKCapstone admit_headroom
returned unsafe-swap. No threshold was weakened.

Created new root-owned mode-0600 /var/lib/skfleetfiberf1be0929, 2147483648
allocated bytes, with 2147479552 usable swap bytes. Native ext4 swap and its
matching systemd unit were validated before activation. An invalid explicit
negative priority was caught during validation and removed before installation.

Rollback rehearsal stopped only var-lib-skfleetfiberf1be0929.swap after checking
available RAM exceeded its used pages plus 1 GiB. The original /swap.img
remained active with exactly the same size, used bytes, and priority; SwapFree
returned to 220 kB. The new unit was then enabled and started. Readback:
ActiveState=active, UnitFileState=enabled, new swap used=0, old swap unchanged.

Installed unit and reviewed source SHA256 both:
`99f704b0a6ff862901fd471a33d70f3a3d35d884372ab79cbd09765350a6c056`.

Fresh probe after activation: headroom=ok, MemAvailable=12060912 kB,
SwapTotal=10485752 kB, SwapFree=2097368 kB. Runtime candidate capacity is two
implementation workers; none has been launched by this deployment.

Rollback procedure remains in RESOURCE-CHANGE.md. Never delete an active swap
file or use swapoff -a. The existing swap file was not cleared or resized.

## Old boot dispatch paths retired

chiap08 skfleet-estate-glm.timer and skfleet-niobe-live.timer changed from
enabled/inactive to disabled/inactive. Unit definitions and worker processes
were preserved. Only their enablement symlinks were removed; `systemctl --user
enable <exact-name>` restores them without starting anything. Do not use --now
until their old safety and authorization gates pass. Other inspected rotation
timers were already disabled/inactive.

The journal proves Niobe first failed with status 70/SOFTWARE at 13:47:33,
restarted at 13:48:00, then received SIGTERM at 13:51:32. The estate service
received SIGTERM at 13:51:50. The terminating actor and full software-failure
cause are not established. Neither old dispatcher was restarted here.

## Correct chiwk12 service identity

SSH as mrarch verified sknoded.service loaded/active, MainPID=974. A live probe
under that same account verifies the process, fresh heartbeat, memory/swap
headroom, and gateway reachability. No duplicate daemon was installed under
skuser01. chiwk12 remains spare capacity, with zero new worker slots activated.

## Admission source and remote verification

Staged fiber_admission.py uses single-authority SQLite reservations, the
existing coordination claim CLI, exact owner/revision readback, and Git's
non-force new-branch worktree creation. It is not yet installed in a dispatcher.

Eleven tests passed locally (0.146 seconds) and on chiap04 (0.079 seconds).
Remote unit f1be0929-admission-tests-X6sJff, invocation
62929f4261044b719fe214a75d2300d9: success, exit 0, runtime 146 ms, CPU 112 ms,
memory peak 2.5M, swap peak 0B. Native limits: CPUQuota=200%, MemoryMax=1G,
MemorySwapMax=256M, TasksMax=64, RuntimeMaxSec=60.

Tests cover nine-worker refusal including legacy names, persistent spacing and
duplicate refusal, simultaneous reservation contenders, stale/future/unknown
observations, unavailable hosts/providers, host caps, stage placement, reviewer
family independence, Kimi refusal, live/reserved deduplication, preservation of
occupied paths and branches, successful new worktree creation, refused claims,
and exact current owner/revision/state readback.

Known limits: reservation lifecycle reconciliation, actual-start spacing,
provider attribution/qualification, and orchestration integration remain to be
completed before use. Boolean fixture readiness is not production evidence.
No tests or source code here authorize bypassing policy, claim, or review gates.

## Live observation, not full deployment proof

LIVE-PREFLIGHT-20260929T204436.json records process/service/heartbeat/headroom
and gateway checks across eight hosts. All eight responded. Initial targets
chiap02/03/04 passed these runtime checks. The 26-process reservation upper bound
includes unclassified processes and coordinator processes; it is not a claim
that 26 implementation agents are working. Herdr separately reports more than
nine named working sessions, so there is no capacity for new worker admission.

The collector does not mark provider routes qualified; its providers map is
empty until a separate exact-route gate is integrated. The existing lane-health
helper separately reported healthy for DeepSeek, Codex, and GLM, with revision
3abf87ce34e064a6b75182304679e60760020c1e. That is not a completion probe or proof
that sk-zai-m qualification is repaired. Kimi remains fenced.

Remaining acceptance: one live dispatcher, shared admission across preserved
stage launch paths, complete reservation recovery and spacing, review before
activation, and the distributed implementation/review/test canary with measured
useful completed work and rework. The goal and card remain active.
