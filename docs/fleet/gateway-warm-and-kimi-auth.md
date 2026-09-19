# Gateway warm-start and kimi credential sync

Two operational scripts for `skgateway-codex` on the chi estate, both of which
close a failure that a healthy-looking gateway does not report.

## The cold-start deadlock

`isAvailable()` is fail-closed. A backend reads `unknown` while `_lastCheck === 0`,
and `_lastCheck` is set only by `recordOutcome()`, which runs AFTER a request has
been served. So a freshly restarted gateway is in a state that cannot clear
itself: admission refuses the backend because its health is unknown, and its
health stays unknown because admission refused it.

Lane admission is fail-closed for the same reason (`lane_health()` refuses
`unknown`), so the deadlock is not cosmetic. Measured on 2026-09-18, a restart
left every backend unobserved and the estate dispatched nothing until a request
was forced through by hand.

`scripts/gateway/skgw-warm` sends one small completion to each backend, which is
the only thing that can set `_lastCheck`. Install it as an `ExecStartPost` so a
restart warms itself:

```
# ~/.config/systemd/user/skgateway-codex.service.d/50-warm-backends.conf
[Service]
ExecStartPost=/bin/bash -lc "sleep 3; %h/.local/bin/skgw-warm || true"
```

The copy in `reference/systemd/50-warm-backends.conf` is the deployed drop-in.

Two details that are load-bearing:

- The warm request sends no `temperature`. The kimi backends reject
  `temperature: 0`, so a warm probe carrying one fails on exactly the backends
  most likely to be cold.
- The gateway forwards the CALLER's `User-Agent` upstream. A probe written with
  python `urllib` earns a Cloudflare 1010 from the kimi upstream, which reads as
  a dead backend and is not. Diagnose with the same user agent the gateway's own
  clients use before concluding a backend is down.

`|| true` keeps a failed warm from failing the unit. A gateway that started is
better than a gateway that refused to.

## Kimi credential sync

`scripts/gateway/sync-kimi-auth.sh` copies the kimi session credential from the
host that refreshed it to the host that serves it, because the refresh happens
where a human logs in and the gateway runs somewhere else.

It refuses more than it accepts, which is the point:

- the source file must parse as JSON and carry BOTH tokens, so a truncated or
  half-written file is never installed
- an expired credential is refused rather than installed, because installing one
  replaces a working credential with a broken one
- NEWEST WINS. If the destination's credential is newer than the source's, the
  script does nothing. Without that rule a periodic sync running the wrong
  direction quietly reverts every refresh.
- the previous credential is backed up before replacement
- the installed file is mode 0600
- the gateway restarts ONLY when the credential actually changed, so running the
  script on a timer does not restart the gateway on every tick

Run it on a timer to keep the token fresh. It is safe to run when nothing has
changed, which is what makes it safe to schedule.

## Verifying, not assuming

Read the state back through a path other than the one that wrote it. After a
restart, confirm each backend reports `observed=true` rather than confirming the
unit is active: a unit that started tells you nothing about whether admission
will let a request through.
