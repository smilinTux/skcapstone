# Host-local fleet liveness publisher rollout candidate

Status: source-only candidate. Do not execute these commands on this
source-repair card.

The `skfleet-live-publisher` unit reads only local tmux sessions, running
`skfleet-worker-*.service` units, and CardStore claim identity. It atomically
publishes `evidence/fleet-live/<host>.json`. It has no card selection, claim,
release, launch, reaper, or dispatch path.

## Explicit namespace-safe pane-evidence contract

The service keeps `PrivateTmp=yes`, so its `/tmp` is a private namespace and
cannot see the default `/tmp/tmux-$UID` socket. Pane evidence is therefore
read only through an explicitly configured socket:
`SKFLEET_TMUX_SOCKET=%t/skfleet/tmux.sock`, created under the shared per-user
runtime directory (`/run/user/$UID`), which `PrivateTmp` does not shadow.
`RuntimeDirectory=skfleet` provisions that directory.

The publisher refuses to run without this explicit socket and fails closed —
it exits nonzero and leaves the last published snapshot untouched — whenever
the socket is missing, is not a socket file, or the probe cannot connect.
`failed to connect` is never accepted as an empty process view, so a hidden
socket can no longer publish false-empty evidence that would satisfy the
all-known-host freshness gate. A truthful empty view requires a reachable
server on the explicit socket that reports zero sessions.

After independent review and a separate human-authorized execution card, stage
the packaged service and timer on every host in the estate's `rotation_hosts`
list. The execution card must also point the worker tmux servers at the same
`%t/skfleet/tmux.sock` so the publisher and workers share one socket. Verify
the files first, then install and enable only
`skfleet-live-publisher.timer`. `skfleet-rotate.timer remains disabled` on every
host; `skfleet-niobe-live.timer remains the sole centralized dispatcher` on its
authorized control host.

Candidate commands for that later execution card:

```bash
install -m 0644 skfleet-live-publisher.service "$HOME/.config/systemd/user/"
install -m 0644 skfleet-live-publisher.timer "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now skfleet-live-publisher.timer
```

Acceptance must show one fresh, host-matching report for every configured host,
with each observed worker carrying its exact owner and claim revision. Missing,
invalid, future, delayed, or stale evidence continues to deny reaper authority.
Rollback disables and removes only `skfleet-live-publisher.timer` and its
service; it does not enable any retired rotation timer or alter centralized
dispatch.
