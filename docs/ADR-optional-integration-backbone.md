# ADR: skcapstone as the Optional Integration Backbone for the sk* Ecosystem

**Status:** Accepted — **backbone implemented 2026-06-09** (consumer adapters pending)
**Author:** Opus (application architect), commissioned by Chef
**Scope:** skcapstone + all sk* consumer services (skmemory, skcomms, skchat, sksecurity, capauth, skvoice, skseed, cloud9, skgateway)

> **Implementation status (2026-06-09):** The full backbone is live and tested in
> skcapstone (208 tests green):
> - `skcapstone.sdk` stable facade — `is_available / alert / register_job /
>   unregister_job / coord_create / register_service` (`src/skcapstone/sdk.py`).
> - Scheduler `jobs.d/` drop-in merge + `register_job()` (`scheduler_jobs.py`:
>   `load_jobs_with_dropins`); runtime callers (daemon scheduled_tasks, scheduler
>   CLI, doctor) repointed to the merged loader.
> - Canonical alert sink: `sdk.alert()` → PubSub topic `<service>.<severity>`
>   (+ notify on warn/error/critical); `skcapstone alerts` now subscribes to
>   `*.critical|*.error|*.warn` and styles consumer topics by severity suffix.
> - Discovery: `sdk.register_service()` writes `~/.skcapstone/registry/<name>.json`;
>   `service_health.check_all_services()` unions the registry with built-in defaults.
>
> Consumers can now code against `skcapstone.sdk` immediately. **skcomm is folding
> into skcomms** (canonical pivot) — target skcomms for that adapter.

---

## 1. Problem

skcapstone already ships the primitives a distributed agent ecosystem needs — alerting
(`PubSub` → Telegram/desktop), a config-driven job scheduler (`scheduler_*`), a coordination
board (`coordination.Board`), and service health checks (`service_health`). **But no consumer
service uses any of them.** A deep-dive audit (2026-06-09) found:

- **Zero hard dependencies.** No sk* repo imports `skcapstone` at runtime; none list it in
  `pyproject.toml`/`package.json`. Good for sovereignty — bad for cohesion.
- **Alerting is fragmented.** skmemory → its own Telegram; skchat → `notify-send`;
  sksecurity → its own dashboard; most → nothing. No common sink.
- **Scheduling is decentralized.** Every service rolls its own: `PromotionScheduler`,
  threading daemons, `while True` loops, per-repo systemd timers / launchd plists. None
  register with skcapstone's scheduler.
- **No discovery.** `service_health` health-checks a *hardcoded* list of services; there is
  no registry a service can join.

The goal Chef set: **every sk* service should, by default, use skcapstone's sk-alert and
skscheduler when skcapstone is installed — and continue to run fully standalone when it is
not.** skcapstone is the integration backbone; consumers are sovereign.

## 2. Design Principles

1. **Optional-by-presence, default-on.** If `skcapstone` importable (or its daemon reachable),
   route through it automatically. If not, fall back to the service's native mechanism. No
   config flag required to get the integrated path — presence *is* the signal. A
   `SK_STANDALONE=1` escape hatch forces native mode even when skcapstone is present.
2. **No new hard dependency.** skcapstone stays an `extras_require`/optional install. Standalone
   must remain a first-class, tested mode.
3. **One stable contract.** Consumers code against a small, frozen public facade
   (`skcapstone.sdk`), never against internal modules (`pubsub`, `scheduler_runner`, …). The
   facade is the only thing we promise not to break.
4. **Graceful degradation is idiomatic** (the `try/except ImportError → None → capability
   check` pattern skcapstone already uses internally).
5. **Polyglot.** Python services import the SDK in-process; Node (`skgateway`) and any
   non-Python service integrate via the skcapstone daemon's HTTP/MCP endpoint.

## 3. Target Architecture

### 3.1 The stable SDK facade — `skcapstone.sdk` (new)

A thin, versioned public module wrapping the existing internals. The **only** surface
consumers import:

```python
# skcapstone/sdk.py  (stable, semver-tracked)
def is_available() -> bool: ...                         # in-proc + daemon presence
def alert(topic: str, payload: dict, *, level: str = "info",
          notify: bool = False) -> bool: ...            # wraps PubSub.publish (+ notify path)
def register_job(spec: dict) -> Path: ...               # writes a jobs.d/ drop-in fragment
def unregister_job(name: str) -> None: ...
def coord_create(title: str, **kw) -> str: ...          # wraps coordination.Board
def register_service(name: str, health_url: str | None = None,
                     pid_file: str | None = None) -> None: ...  # discovery
```

Implementation note: `alert()` wraps `PubSub.publish(topic, payload)` and, when `notify=True`
or `level` ≥ warn, the existing notification/Telegram path. `register_job()` solves the
"scheduler is config-only" gap (see 3.2).

### 3.2 Scheduler drop-in registration — `jobs.d/`

Today jobs live in a single static `~/.skcapstone/config/jobs.yaml`. External services cannot
self-register. Add a **conf.d-style drop-in directory**: `load_jobs()` merges
`~/.skcapstone/config/jobs.yaml` **plus** every `~/.skcapstone/config/jobs.d/*.yaml`. A service
calls `sdk.register_job({...})` which writes `jobs.d/<service>.yaml`. Standalone services keep
their native systemd timer; integrated services let the skcapstone daemon own the cadence
(single scheduler, central retry/notify/jitter, cross-host `nodes:` placement).

### 3.3 Consumer-side adapter (the pattern every repo implements)

```python
# <service>/integration.py
import os
try:
    from skcapstone import sdk as _sk
    _HAS = (not os.environ.get("SK_STANDALONE")) and _sk.is_available()
except ImportError:
    _sk, _HAS = None, False

def alert(topic, payload, level="info"):
    if _HAS:
        return _sk.alert(f"{SERVICE}.{topic}", payload, level=level, notify=level in ("warn","error","critical"))
    return _native_alert(topic, payload, level)   # existing Telegram/notify-send/log

def ensure_schedule():
    if _HAS:
        _sk.register_job(_job_spec())             # central scheduler owns cadence
    else:
        _ensure_native_timer()                    # existing systemd/thread loop
```

### 3.4 Discovery

Extend `service_health` from a hardcoded list to a registry: services call
`sdk.register_service(name, health_url=…, pid_file=…)`, which writes
`~/.skcapstone/registry/<name>.json`; `check_all_services()` unions the registry with the known
defaults. Optional — health still works without it.

### 3.5 Polyglot bridge (skgateway / Node)

Non-Python services hit the daemon's HTTP/MCP endpoint (`pubsub_publish`, `coord_*` MCP tools
already exist) behind the same presence check (daemon reachable on `:9383`/`:9475`?). Same
default-on / fall-back-to-native semantics.

## 4. Integration Contract (frozen surface)

| Capability | Integrated path (skcapstone present) | Standalone fallback |
|---|---|---|
| Alert / notify | `sdk.alert()` → PubSub topic `<svc>.<sev>` → notify/Telegram | service-native (Telegram / notify-send / log) |
| Scheduled work | `sdk.register_job()` → `jobs.d/<svc>.yaml` → daemon scheduler | service-native systemd timer / thread loop |
| Coordination | `sdk.coord_create()` | n/a (no-op / local log) |
| Discovery/health | `sdk.register_service()` → registry | service-native pid/log |
| Topic naming | `<service>.<severity>` (e.g. `skmemory.error`) | — |
| Escape hatch | `SK_STANDALONE=1` forces native even if present | default |

## 5. Acceptance (system-level)

- Each consumer passes its test suite **with skcapstone absent** (import-time and runtime).
- With skcapstone present, alerts land on PubSub topics and jobs appear in `skcapstone scheduler list`.
- No consumer adds a hard `skcapstone` dependency; integration lives behind `extras_require`/optional import.
- `SK_STANDALONE=1` forces native mode end-to-end.

## 6. Work Breakdown (see coord board, tag `sk-integration`)

- **Backbone (skcapstone):** `skcapstone.sdk` facade · `jobs.d/` drop-in + `register_job()` ·
  canonical alert sink + topic convention · service registry in `service_health`.
- **Per-consumer adapters (sonnet):** skmemory · skcomm · skchat · sksecurity · capauth ·
  skvoice · skseed · cloud9 · skgateway (Node HTTP bridge).
- **Cross-cutting:** dual-mode integration test harness (absent/present) · per-repo README
  "integration modes + `~/.skcapstone/` filesystem contract" section.
