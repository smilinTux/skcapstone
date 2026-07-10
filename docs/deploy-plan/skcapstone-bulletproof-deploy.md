# skcapstone Bulletproof Deployment Plan

Status: PROPOSED (2026-07-09)
Repo: `~/clawd/skcapstone-repos/skcapstone` (main, pyproject version 0.14.0)
Definition of bulletproof: reproducible from scratch on a new machine, secrets never in git, HA with no single point of failure ("if you need one, get two"), CI-gated, observable, self-recovering, and documented well enough that a cold machine can stand it up.

## 1. Current State

The runtime layer is mid-maturity with real production plumbing:

- Daemon is `Type=notify` with a hand-rolled `sd_notify(3)`, `WatchdogSec=300`, and a ComponentManager that heartbeat-monitors and auto-restarts the core loops (poll/health/sync/housekeeping/healing) with `MAX_RESTARTS=5`.
- Startup preflight is enforced, not advisory: daemon start refuses to run on critical preflight failures. Standalone `skcapstone preflight --json-out` and a 1676-line `doctor.py` with 13+ check families and `--fix` exist.
- Test suite is real: 3994 tests collect cleanly, 163 test files, and `.github/workflows/pytest.yml` is honest CI (strict markers, no masking).
- Secrets posture is clean: no committed credentials found in targeted scans; `.env` is gitignored, `.env.example` is all placeholders, key material is delegated to capauth.
- Install story has the right bones: `~/.skenv` venv, editable-or-PyPI fallback, `verify_install.sh` fresh-venv smoke test, GFS backup script plus `docs/BACKUP.md`, Prometheus metrics landed.

But the exact failure modes already seen in production on .41 are still encoded in the repo, and the release pipeline is not actually gated. Verified in-repo on 2026-07-09:

- `systemd/skcapstone@.service` (the per-agent template the fleet uses) has NO `MemoryMax`, no `StartLimitIntervalSec`/`StartLimitBurst`, and a fixed `RestartSec=10` loop. The .41 OOM fix (systemd-oomd plus manual caps) lives only as hand-applied host state.
- Two divergent systemd unit trees: `systemd/` (used by `scripts/install.sh`, `.skenv` paths, relaxed hardening) vs `src/skcapstone/data/systemd/` (used by `skcapstone daemon install`, `%h/.local/bin` ExecStart that will not exist under the `.skenv` convention, plus `ProtectSystem=strict` and `ProtectHome=read-only` that the top-level units explicitly removed as known-breaking).
- `publish.yml`: test job is `continue-on-error: true` and both publish jobs are `needs: test` plus `if: always()`, so a tag push ships to PyPI and npm even when the whole suite fails. `ci.yml` masks its core test run and lint with `|| true`.
- Version triple drift: pyproject `0.14.0`, `src/skcapstone/__init__.py` `__version__ = "0.13.0"`, `package.json` `0.12.5` (README also says 0.13.0).
- `AGENT_PORTS` maps opus, lumina, AND jarvis all to 9383; any unknown agent gets 9384 (skcomms' documented port). On bind failure the daemon logs and continues WITHOUT its API server, silently blinding monitoring on multi-agent boxes.
- `coordination.py` and `itil.py` write JSON with plain `write_text` (no tmp plus `os.replace`); an OOM-kill mid-write leaves truncated JSON that Syncthing replicates fleet-wide and the board silently skips. `pubsub.py` already has the correct atomic pattern (line 157).
- The watchdog is blind to component death: `_health_loop` pings `WATCHDOG=1` unconditionally, so a dead consciousness loop or an exhausted-restarts poll loop leaves a daemon that systemd reports healthy.
- `scripts/install.sh` cannot run unattended (interactive prompts, no `--yes`), suppresses all pip errors, enables a nonexistent `skcapstone-context.timer`, and falls back to `alias claude='claude --dangerously-skip-permissions'` in shell rc files.

## 2. Target: What Bulletproof Means for This Repo

Concretely, a machine with nothing but the OS, Syncthing, and this repo (or PyPI) must reach a healthy multi-agent daemon with one documented command sequence, and stay healthy unattended.

1. **Reproducible from scratch.** One canonical systemd unit set, deployed identically by both install paths (`scripts/install.sh` and `skcapstone daemon install`). `install.sh --yes` runs unattended end to end. A lockfile pins `~/.skenv` contents so .158 and .41 cannot silently diverge. A cold-machine runbook is tested against a fresh container or VM.
2. **Secrets never in git.** Maintain the current clean posture (already good); keep secret retrieval delegated to capauth/SKStacks, document `SKSTACKS_V2_PATH`, and keep the secret-returning MCP tools audited.
3. **HA, no SPOF.** The per-agent daemon is a deliberate singleton per box, so the redundancy mantra applies at the fleet level: peer heartbeats must page (sk-alert/Telegram/webhook) when an agent goes dark, restart storms must alert, and the coordination board must survive crashes (atomic writes) and Syncthing conflicts without losing claims.
4. **CI-gated.** No release reaches PyPI or npm without the honest test suite passing. `ci.yml` stops lying (`|| true` removed or the workflow retired in favor of pytest.yml as the required check). Tag version must match all three version sources.
5. **Observable.** systemd watchdog reflects real component health (dead brain = watchdog trip or explicit degraded state plus alert). `doctor`/`preflight` can see the runtime unit layer: unit drift, missing MemoryMax, NRestarts storms, port conflicts, coordination-store corruption.
6. **Self-recovering.** Restart policy has backoff and a start limit with an `OnFailure=` alert hook instead of a silent infinite 10s loop. Memory caps in the unit files, not host state. Housekeeping prunes per-event notification files before they become the next 140k-file flood.
7. **Documented.** `docs/deploy-plan/` runbook: install, verify, upgrade, rollback, and disaster recovery, all validated on a cold machine.

## 3. Gap Analysis (severity-ordered)

| # | Severity | Area | Gap |
|---|----------|------|-----|
| G1 | critical | Release gating | `publish.yml` test job `continue-on-error: true` plus publish jobs `if: always()`: broken releases ship to PyPI/npm on any tag. `ci.yml` masks tests and lint with `\|\| true`. |
| G2 | critical | systemd memory caps | `skcapstone@.service` has no `MemoryMax`/CPU caps in either copy. The exact hole that thrashed .41; every new machine re-inherits it. |
| G3 | critical | systemd unit drift | `systemd/` vs `src/skcapstone/data/systemd/` diverge materially: packaged units point at `%h/.local/bin` (wrong under `.skenv`), re-enable `ProtectHome=read-only` (known-breaking), and lack the SKAGENT env block. `skcapstone daemon install` deploys the broken set. |
| G4 | high | Restart policy | `Restart=on-failure` + fixed `RestartSec=10`, no `StartLimit*`, no backoff: a failed preflight crash-loops forever with no alert. |
| G5 | high | Watchdog blind spot | `WATCHDOG=1` pinged unconditionally; passive components (consciousness loop, scheduler) are never restarted and exhausted auto-restart components just "give up" with a log line. Zombie daemons look healthy. |
| G6 | high | API port collisions | opus/lumina/jarvis all map to 9383; unknown agents get 9384 (skcomms). Bind failure is swallowed and the daemon runs blind. Stale `skcapstone-api.socket` hardcodes 7777. |
| G7 | high | Store write integrity | `coordination.py` and `itil.py` use non-atomic `write_text` (7 sites); truncated JSON propagates via Syncthing and claims silently vanish. mtime last-writer-wins conflict resolution can discard claim history. |
| G8 | medium | Install robustness | `install.sh` is interactive-only, swallows pip failures, enables a nonexistent timer, and installs a `--dangerously-skip-permissions` alias as a fallback. |
| G9 | medium | Reproducibility | No lockfile, no documented upgrade/rollback path; fleet upgrades are hand-run `pip install -e` per box (drift already bit .158/.41). |
| G10 | medium | Doctor/preflight coverage | Zero systemd-layer checks (unit drift, caps, NRestarts, port conflicts) and no coordination-store integrity check. Both known .41 failure classes are invisible to the tools. |
| G11 | medium | Headless alerting / HA | No in-repo out-of-band alert (Telegram/webhook) for daemon death, restart storms, or pillar degradation; desktop notifications only, off by default. Peer heartbeat has no paging behavior. |
| G12 | medium | Version drift | 0.14.0 / 0.13.0 / 0.12.5 across pyproject, `__init__.py`, package.json; README says 0.13.0. `--version` never matches PyPI. |
| G13 | low | Notification file growth | One JSON file per notification under `agents/<name>/skcomms/notifications/` with no pruning: same shape as the 140k-file skcomms outbox incident. |
| G14 | low | MCP launcher drift | `scripts/mcp-serve.sh` venv discovery never checks `~/.skenv`, the blessed install target. |
| G15 | low | Secrets tool surface | `capauth_secret_get`/`skstacks_secret_get` return plaintext to any MCP client with no caller scoping or visible audit write; `SKSTACKS_V2_PATH` undocumented in `.env.example`. |

## 4. Remediation Roadmap

### Phase 0: Stop the bleeding (all three parallelizable, no dependencies)

- **P0.1 Gate releases** (G1). Fix `publish.yml` gating, unmask `ci.yml`. Highest leverage: prevents shipping every other bug fleet-wide.
- **P0.2 Unify systemd units** (G3). One source of truth; `daemon install` and `install.sh` deploy byte-identical, `.skenv`-pathed, working units.
- **P0.3 Atomic store writes** (G7). Port the pubsub tmp+`os.replace` pattern to coordination.py and itil.py. Independent of the unit work.

### Phase 1: Encode the .41 fixes in the repo (after P0.2 so edits land in one place)

- **P1.1 Memory caps plus restart backoff** (G2, G4). `MemoryMax`, `StartLimitIntervalSec`/`Burst`, `OnFailure=` alert hook in the canonical template. Depends on P0.2.
- **P1.2 Watchdog honesty** (G5). Component health gates the `WATCHDOG=1` ping; give-up and passive-component death become loud. Parallelizable with P1.1.
- **P1.3 Port assignment fix** (G6). Unique deterministic per-agent ports, loud bind failure, retire or fix the stale socket unit. Parallelizable with P1.1/P1.2.

### Phase 2: Cold-machine path (install, versions, reproducibility)

- **P2.1 Unattended install hardening** (G8). `--yes` mode, surfaced pip failures, remove the dangerous alias fallback and the ghost timer. Depends on P0.2 (installs the canonical units).
- **P2.2 Version single-sourcing** (G12). One version source, publish gate verifies all artifacts. Parallelizable.
- **P2.3 Lockfile plus upgrade/rollback docs** (G9). Parallelizable with P2.1/P2.2.

### Phase 3: Observability and self-diagnosis

- **P3.1 Doctor/preflight systemd and store checks** (G10). Needs the canonical units (P0.2) and caps (P1.1) to know what "correct" looks like.
- **P3.2 Headless out-of-band alerting** (G11). Webhook/Telegram transport wired to daemon death (`OnFailure=`), restart storms, pillar degradation, and peer-dark heartbeat events. Pairs with P1.1's `OnFailure=` hook.

### Phase 4: Hygiene and validation

- **P4.1 Notification pruning in housekeeping** (G13). Parallelizable, small.
- **P4.2 MCP launcher `.skenv` discovery plus README version fix** (G14, part of G12). Parallelizable, small.
- **P4.3 Cold-machine standup runbook, validated end to end** (Definition item 7). Depends on P0.2, P1.1, P2.1: the runbook must describe the fixed system, and its validation is the acceptance test for the whole initiative.

G15 (secrets tool scoping) is tracked but deferred to the skvault/PQC security workstream; it is an access-control improvement, not a deploy blocker, and the current posture (no committed secrets) meets the bulletproof bar.

## 5. Task List

Each task is sized for one subagent (hours, not weeks). Dependencies reference exact task titles.

1. **skcapstone: gate PyPI/npm publish on green tests** (critical, no deps)
   Fix `.github/workflows/publish.yml` (drop `continue-on-error`, replace `if: always()` with success-gated publish) and unmask `.github/workflows/ci.yml` (`|| true` on tests and lint) or retire ci.yml's test job in favor of pytest.yml as the required check.
2. **skcapstone: unify systemd units into one source of truth** (critical, no deps)
   Make `src/skcapstone/data/systemd/` and `systemd/` byte-identical (generated or synced at build time), with `.skenv` ExecStart paths, relaxed hardening, and the SKAGENT env block everywhere; add a test that fails on drift.
3. **skcapstone: add MemoryMax and restart backoff to the agent unit template** (critical, depends on task 2)
   Encode the .41 host fixes in `skcapstone@.service`: `MemoryMax`, `StartLimitIntervalSec`/`StartLimitBurst`, and an `OnFailure=` alert hook so crash-loops terminate and page instead of spinning forever.
4. **skcapstone: atomic writes for coordination and ITIL stores** (high, no deps)
   Apply the pubsub tmp+`os.replace` pattern to all 7 non-atomic write sites in `coordination.py` and `itil.py` (plus the pubsub subs file), so OOM-kills cannot propagate truncated JSON fleet-wide.
5. **skcapstone: wire component health into the systemd watchdog** (high, no deps)
   Gate the `WATCHDOG=1` ping on required-component health; make give-up after MAX_RESTARTS and passive-component death loud (degraded status plus alert event) instead of silent.
6. **skcapstone: fix multi-agent API port assignment** (high, no deps)
   Unique deterministic per-agent ports (stop mapping opus/lumina/jarvis all to 9383, stop handing 9384/skcomms to unknown agents), fail loudly on bind failure, retire or fix `skcapstone-api.socket`.
7. **skcapstone: harden install.sh for unattended cold-machine installs** (medium, depends on task 2)
   `--yes`/non-interactive mode, surface pip failures, remove the `claude --dangerously-skip-permissions` alias fallback and the nonexistent `skcapstone-context.timer` enable.
8. **skcapstone: single-source the package version** (medium, no deps)
   Fix the 0.14.0/0.13.0/0.12.5 triple drift; derive `__version__` and package.json from pyproject (or a sync check in CI plus publish gate on all three), fix README.
9. **skcapstone: lockfile and documented upgrade/rollback procedure** (medium, no deps)
   Add a pinned lockfile for the `~/.skenv` install path and write `docs/UPGRADE.md` (upgrade, rollback, fleet reconcile).
10. **skcapstone: doctor and preflight checks for the systemd runtime layer** (medium, depends on tasks 2 and 3)
    New check families: installed-unit vs bundled-unit drift, MemoryMax/StartLimit presence, NRestarts restart-storm detection, API port conflicts, and coordination-store integrity (truncated/malformed JSON scan).
11. **skcapstone: headless out-of-band alerting for daemon and agent-dark events** (medium, depends on task 3)
    In-repo webhook/Telegram alert transport fired by the `OnFailure=` hook, restart storms, pillar degradation, and peer heartbeat going dark, so headless boxes page instead of failing silently.
12. **skcapstone: prune per-event notification files in housekeeping** (low, no deps)
    Bound `agents/<name>/skcomms/notifications/` growth (age/count-based pruning in the housekeeping loop) before it repeats the 140k-file outbox incident under Syncthing.
13. **skcapstone: teach mcp-serve.sh about ~/.skenv** (low, no deps)
    Add `~/.skenv` to the venv discovery candidates so the MCP launcher works on blessed installs without manual `SKCAPSTONE_VENV`.
14. **skcapstone: cold-machine standup runbook, validated end to end** (high, depends on tasks 2, 3, and 7)
    Write and validate a start-to-healthy runbook (fresh container/VM: install, preflight, daemon up, doctor green, upgrade and rollback exercised); this is the acceptance test for the initiative.
