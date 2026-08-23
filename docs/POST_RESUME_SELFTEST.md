# Post-Resume Self-Test

Laptop-fleet machines (notably the `.41` laptop) suspend and resume. After a
resume the sovereign stack can be left in a bad state: the daemon may have
wedged, memory / skmem-pg connections may have dropped, the coordination board
may be unreadable, the comms transport may be dead, tokens may have expired, or
the wall clock may have skewed while the machine slept.

`skcapstone selftest post-resume` is an automated, **read-only** check that
verifies the stack is healthy after the machine wakes, reports a structured
pass/fail per check plus an overall status, exits non-zero on any critical
failure, and (opt-in) can emit an alert so a resumed laptop flags issues.

It is deliberately observational. A self-test observes; it does not mutate.
Any self-heal is left to explicit, conservative tooling
(`skcapstone daemon start`). This command only reports and, when enabled, alerts.

## What it checks

It **reuses** the existing health machinery rather than reinventing it:

| Check | Source | Critical by default |
|-------|--------|---------------------|
| `daemon` (process alive) | `daemon.is_running` (PID file + `kill -0`) | yes |
| `identity:*` (identity / token validity) | `doctor.run_diagnostics`, category `identity` | yes |
| `memory:*` (memory / skmem-pg reachable) | `doctor.run_diagnostics`, category `memory` | yes |
| `transport:*` (comms transport alive) | `doctor.run_diagnostics`, category `transport` | yes |
| `coordination_board` (board readable) | `coordination.Board.load_tasks` | yes |
| `sync:*` (sync state) | `doctor.run_diagnostics`, category `sync` | no (warn) |
| `clock_skew` (wall-clock drift after resume) | monotonic-vs-wall probe (injectable) | no (warn) |
| `network` (tailscale / reachability) | `tailscale status` (read-only, injectable) | no (warn) |

Doctor categories outside the configured critical/warn sets (e.g. `harness`,
`codex`) are intentionally ignored: they are not resume-relevant.

Overall status is `fail` when any **critical** check fails (exit code `1`),
`warn` when only non-critical checks fail or warn (exit code `0`), and `pass`
otherwise (exit code `0`).

## Usage

```bash
skcapstone selftest post-resume            # colored table, exit 0/1
skcapstone selftest post-resume --json-out # structured JSON report
skcapstone selftest post-resume --alert    # force alert-on-failure this run
skcapstone selftest post-resume --quiet    # exit code only (for hooks)
```

## Configuration

Optional, config-driven thresholds live in
`~/.skcapstone/config/selftest.yaml` (or the per-agent home). Safe defaults
apply when the file is absent. Malformed config falls back to defaults - a
self-test never crashes on bad config.

```yaml
# ~/.skcapstone/config/selftest.yaml
critical_categories: [identity, memory, transport]  # doctor categories that are fatal
warn_categories: [sync, agent, packages]            # surfaced but non-fatal
check_daemon: true
check_board: true
check_clock: true
check_network: true
clock_skew_warn_seconds: 120     # skew above this warns
clock_skew_fail_seconds: 0       # >0 and skew above it fails (add "clock" to
                                 # critical_categories to make it fatal)
network_host: ""                 # informational hint for the network probe
alert_enabled: false             # opt-in; a critical failure emits an alert
```

Alerts reuse the existing notification transport
(`skcapstone.notifications.notify`). They fire **only** when `alert_enabled`
is true (or `--alert` is passed) **and** the overall result is a critical
failure.

## Wiring it to a systemd suspend/resume hook (opt-in)

This repo ships the hooks as **inert, version-controlled templates** but does
**not** install or enable any live hook. `scripts/install.sh` never copies them,
so an install leaves your machine untouched until you opt in. Two supported
patterns, both templated in the tree. Pick one.

### Option A - `systemd --user` unit ordered after `suspend.target`

Template: `systemd/skcapstone-post-resume.service` (mirrored into the wheel at
`src/skcapstone/data/systemd/`, same as the other unit templates). It runs as
your user on resume, so it has access to your agent home and desktop
notification bus. Install it yourself:

```bash
install -m 0644 systemd/skcapstone-post-resume.service \
    ~/.config/systemd/user/skcapstone-post-resume.service
systemctl --user daemon-reload
systemctl --user enable skcapstone-post-resume.service
```

The unit is a `Type=oneshot` ordered `After=` and `WantedBy=` the sleep targets
(`suspend`/`hibernate`/`hybrid-sleep`/`suspend-then-hibernate`), so systemd
starts it once on every wake. Drop `--alert` from its `ExecStart` if you prefer
to gate alerting purely through `selftest.yaml` (`alert_enabled: true`).

### Option B - system `systemd-sleep` hook

Template: `scripts/system-sleep/50-skcapstone-selftest`. system-sleep hooks run
as root around every suspend/hibernate; the script acts only on the `post`
(wake) phase and drops to the agent's user so the agent home and notification
bus resolve. Its trailing `|| true` keeps a failed self-test from blocking
anything (it already flags via `--alert` and its exit code). Install it as root:

```bash
sudo install -m 0755 scripts/system-sleep/50-skcapstone-selftest \
    /usr/lib/systemd/system-sleep/50-skcapstone-selftest
```

Set `SK_USER` at the top of the script (or export it) if the auto-detected
login user is wrong for your host.

To review outcomes:

```bash
journalctl --user -u skcapstone-post-resume.service     # Option A
skcapstone selftest post-resume --json-out              # ad-hoc
```
```
