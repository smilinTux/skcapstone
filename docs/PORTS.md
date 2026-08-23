# SKCapstone daemon status-API ports

Each agent daemon (`skcapstone daemon start --agent <name>`) exposes a local
HTTP status/health API on `127.0.0.1`. When two or more agents run on one host
(via the `skcapstone@` systemd template) they **must** bind distinct ports, or
the second daemon's bind fails and it runs blind (no status/health monitoring).

Ports are assigned in `src/skcapstone/__init__.py` and resolved by
`_resolve_agent_port` in `src/skcapstone/cli/daemon.py`.

## Assignment rules

1. **Explicit `--port`** always wins.
2. **Known agents** get a distinct, deterministic port from `AGENT_PORTS`:

   | Agent  | Port | Notes |
   |--------|------|-------|
   | lumina | 9383 | documented in the fleet `~/.skcapstone/docs/PORTS.md` as `skcapstone@lumina`; kept stable |
   | opus   | 9389 | free slot between skchat-opus (9388) and the signaling broker (9390) |
   | jarvis | 9391 | above the signaling broker; clear of jarvis-heartbeat (9387) |

3. **Unknown agents** get a stable, restart-deterministic hash-based port in a
   dedicated dynamic range (`9400-9499`, `hashed_agent_port`). The hash uses
   SHA-256 (not Python's per-process-salted `hash()`) so the same agent always
   maps to the same port. The range sits above the fleet band, so an unknown
   agent never lands on a documented service port.
4. **No-agent (single daemon)** keeps the package `DEFAULT_PORT` (9383).

## Reserved fleet ports (never auto-assigned)

`FLEET_RESERVED_PORTS` mirrors the fleet `~/.skcapstone/docs/PORTS.md`. The
daemon must never auto-assign onto one of these:

| Port | Service |
|------|---------|
| 9384 | skcomms federation S2S API |
| 9385 | skchat daemon (lumina) health/metrics |
| 9386 | sk-access MCP (tailnet) |
| 9387 | jarvis-heartbeat |
| 9388 | skchat daemon (opus) health/metrics |
| 9390 | skcomms signaling broker |
| 9392 | `sknoded` operator-plane HTTP surface (`/operator/v1/...`, tailnet-bind only, gated OFF by default via `SKOPERATOR_HTTP` -- see `docs/OPERATOR_PLANE_REMOTE_STANDARD.md`) |

> The original bug (card 36d11ec3): `AGENT_PORTS` mapped opus, lumina, and
> jarvis all to 9383, and the unknown-agent fallback handed out `max+1 = 9384`
> — skcomms' federation port. Two agents on one host collided; the losing
> daemon caught the bind error and continued **without** its API server,
> silently losing status/health monitoring.

## Bind-time behavior

`DaemonService._bind_api_server` binds the intended port. On a port collision
(`EADDRINUSE`) it scans the dynamic range for the next free, non-fleet port so
the API stays up rather than going dark. The resulting health is reported in
the daemon status snapshot under `api_server`:

- `ok` — bound on the intended port.
- `rebound` — bound on a fallback port after a collision (**degraded**): emits
  an alert-severity activity event and an `ALERT:` error entry.
- `down` — could not bind at all (**degraded**): emits an alert-severity event
  and an `ALERT:` error entry.

`DaemonState.is_degraded()` returns True for `rebound`/`down`.

## Retired

`skcapstone-api.socket` (hardcoded `127.0.0.1:7777`) was retired. The daemon
never used systemd socket activation and 7777 matched no real service. It is
no longer installed, but `uninstall_service` still removes it from hosts that
installed the old unit.
