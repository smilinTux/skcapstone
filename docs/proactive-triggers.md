# Proactive Trigger Daemon

`skcapstone.triggers` fires actions when a **state condition** becomes true,
rather than on a time schedule. It is the state-driven counterpart to
`skcapstone.scheduled_tasks` (interval / cron).

    <condition over a read-only state snapshot>  ->  <action (notify)>

## Why it is additive, not a parallel daemon

The engine deliberately reuses the framework's existing seams:

- **Evaluation** rides the existing `TaskScheduler` tick via
  `register_with_scheduler()` - no second background thread when wired into the
  main `skcapstone` daemon.
- **Firing** goes through the existing desktop-notification path
  (`skcapstone.notifications.notify`), inheriting its content-dedup and its
  opt-in `SKCAPSTONE_DESKTOP_NOTIFY` guard.
- **Config** mirrors the `jobs.yaml` / `jobs.d` convention: a `triggers.yaml`
  with an optional `triggers.d/*.yaml` drop-in overlay, parsed with the same
  duration parser.

## Rule model

A rule is one entry under the top-level `triggers:` mapping. Two condition
forms:

- **Declarative** - `metric` + `op` + `value`. The engine reads
  `context[metric]` and compares it to `value`. A missing metric evaluates
  `False` (fail-safe: absent data never fires).
- **Named** - `condition: <name>`, a predicate registered in-process with
  `register_condition(name, fn)`. The escape hatch for real logic. An unknown
  name evaluates `False`.

`op` is one of `< <= > >= == !=` (word aliases `lt le gt ge eq ne` also work).

Actions:

- `notify` (default) - a desktop popup via the notification layer.
- `callback` (opt-in) - a `module:function` invoked as `fn(context)`.

Idempotent firing is enforced twice: a per-rule `cooldown` window plus the
notification layer's own content-dedup (keyed `trigger:<name>`). A condition
that stays true does not re-fire until the cooldown lapses.

Safety: config is declarative data only - there is **no** `eval`/`exec` of
config text, and the only default action is a notification.

See `config/triggers.yaml.example` for a worked config.

## Wiring into the main daemon (recommended)

```python
from skcapstone.triggers import (
    TriggerEngine, load_triggers_with_dropins,
    default_config_path, register_with_scheduler, system_metrics_context,
)

engine = TriggerEngine(load_triggers_with_dropins(default_config_path()))
register_with_scheduler(
    scheduler,                       # the daemon's existing TaskScheduler
    engine,
    context_provider=system_metrics_context,
    interval_seconds=30,
)
```

The `context_provider` is any zero-arg callable returning the current state
snapshot; keep it read-only by convention.

## Standalone (optional)

For a triggers-only host, `python -m skcapstone.triggers` runs the engine on its
own `TaskScheduler` (reused, not reinvented) until `SIGINT`/`SIGTERM`. An inert
systemd unit is provided at `systemd/skcapstone-triggers.service` - it is a
template, not installed or enabled by the installer.

## Built-in context provider

`system_metrics_context()` returns a best-effort, read-only snapshot:
`disk_free_pct`, `disk_free_gb`, `mem_available_pct`, `load_avg_1m`,
`cpu_count`. Each probe is independently guarded, so an unavailable metric is
simply omitted (rules referencing it fail safe).
