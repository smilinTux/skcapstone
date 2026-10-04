- `systemd/skmeter.service` ran `/home/cbrd21/.skenv/bin/python`, a path that
  exists on one workstation only. Since rollout installs every shipped unit,
  the readiness gate failed on every chi host. It now uses `%h`.
