"""Proactive trigger daemon - condition -> action rules evaluated on a tick.

Where :mod:`skcapstone.scheduled_tasks` fires work on a *time* schedule
(interval / cron), this module fires work on a *state* condition.  A trigger is
a small declarative rule::

    <condition over a state snapshot>  ->  <action (notify)>

The engine is intentionally additive and reuses the framework's existing seams
rather than inventing a parallel daemon:

* **Evaluation** rides the existing :class:`skcapstone.scheduled_tasks.TaskScheduler`
  tick (see :func:`register_with_scheduler`) - no second background thread.
* **Firing** goes through the existing desktop-notification path
  (:func:`skcapstone.notifications.notify`), inheriting its content-dedup and
  its opt-in ``SKCAPSTONE_DESKTOP_NOTIFY`` guard.
* **Config** mirrors the ``jobs.yaml`` / ``jobs.d`` convention: a ``triggers.yaml``
  with an optional ``triggers.d/*.yaml`` drop-in overlay.

Safety posture (v1):

* Conditions are declarative comparisons (``metric``/``op``/``value``) against a
  read-only state snapshot, or a *named* condition registered in-process via
  :func:`register_condition`.  There is **no** ``eval``/``exec`` of config text.
* The only built-in action is ``notify`` (a desktop popup).  A ``callback``
  action exists but is strictly opt-in and never the default.
* Firing is idempotent: a per-rule ``cooldown`` window plus the notification
  layer's own dedup stop a still-true condition from spamming on every tick.

Typical usage::

    from skcapstone.triggers import TriggerEngine, load_triggers

    rules = load_triggers(Path("~/.skcapstone/config/triggers.yaml").expanduser())
    engine = TriggerEngine(rules)
    fired = engine.tick({"disk_free_pct": 6.0})   # -> ["disk-low"]
"""

from __future__ import annotations

import logging
import operator
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .scheduler_jobs import _parse_duration

logger = logging.getLogger("skcapstone.triggers")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# A state snapshot handed to the engine each tick: metric name -> value.
Context = dict[str, Any]

# A named condition: takes the context, returns True when the trigger should fire.
ConditionFn = Callable[[Context], bool]

# An action sink: (title, body, urgency, dedup_key) -> dispatched?  Mirrors
# skcapstone.notifications.notify so the default sink is a drop-in.
ActionSink = Callable[..., bool]


# Comparison operators allowed in a declarative ``when`` spec.  No eval: the
# config only ever selects one of these by name.
_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
    "eq": operator.eq,
    "ne": operator.ne,
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
}


# ---------------------------------------------------------------------------
# Named condition registry (in-process, opt-in for python-defined conditions)
# ---------------------------------------------------------------------------

_CONDITION_REGISTRY: dict[str, ConditionFn] = {}


def register_condition(name: str, fn: ConditionFn) -> None:
    """Register a named condition callable for use by ``condition:`` rules.

    A rule may reference a python-defined predicate by name instead of a
    declarative ``metric``/``op``/``value`` comparison.  This is the escape
    hatch for conditions that need real logic (a threshold across several
    metrics, a rate-of-change, an external probe result already placed in the
    context).  The function must accept the context dict and return a bool; it
    must not raise (the engine guards against it, but a clean predicate keeps
    logs quiet).

    Args:
        name: The identifier a rule's ``condition`` field will reference.
        fn: Predicate ``(context) -> bool``.

    Raises:
        ValueError: If ``name`` is empty.
    """
    if not name:
        raise ValueError("register_condition: name must be non-empty")
    _CONDITION_REGISTRY[name] = fn


def unregister_condition(name: str) -> bool:
    """Remove a previously registered named condition.

    Args:
        name: The condition name to drop.

    Returns:
        ``True`` if a condition was registered under ``name``, else ``False``.
    """
    return _CONDITION_REGISTRY.pop(name, None) is not None


def _lookup_condition(name: str) -> Optional[ConditionFn]:
    """Return the registered condition for ``name`` (or ``None``)."""
    return _CONDITION_REGISTRY.get(name)


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


@dataclass
class TriggerRule:
    """A single proactive trigger: a condition that fires an action.

    Exactly one of two condition forms is used:

    * **Declarative** - ``metric`` + ``op`` + ``value``: the engine reads
      ``context[metric]`` and compares it to ``value`` with ``op``.  A missing
      metric evaluates ``False`` (fail-safe: absent data never fires).
    * **Named** - ``condition``: the name of a predicate registered via
      :func:`register_condition`.  An unknown name evaluates ``False``.

    Attributes:
        name: Unique rule identifier (the YAML key).
        metric: Context key to read for a declarative comparison.
        op: Comparison operator (one of ``< <= > >= == !=`` or word aliases).
        value: Right-hand side of the declarative comparison.
        condition: Name of a registered predicate (alternative to ``metric``).
        action: Action to fire - ``"notify"`` (default) or ``"callback"``.
        title: Notification title (defaults to the rule name).
        body: Notification body text.
        urgency: Notification urgency - ``low`` | ``normal`` | ``critical``.
        callback: ``module:function`` fired for ``action="callback"`` (opt-in).
        cooldown_seconds: Minimum seconds between two fires of this rule.
        enabled: Whether the rule is evaluated at all.
    """

    name: str
    metric: Optional[str] = None
    op: Optional[str] = None
    value: Any = None
    condition: Optional[str] = None
    action: str = "notify"
    title: Optional[str] = None
    body: str = ""
    urgency: str = "normal"
    callback: Optional[str] = None
    cooldown_seconds: float = 300.0
    enabled: bool = True
    # Populated by the engine, not by config:
    last_fired: Optional[float] = field(default=None, compare=False)
    fire_count: int = field(default=0, compare=False)

    def evaluate(self, context: Context) -> bool:
        """Return whether this rule's condition holds for ``context``.

        Never raises: any evaluation error (bad operator, non-comparable
        types, a raising named predicate) is logged and treated as *not
        firing*, so a malformed rule can never crash the tick or spam an alert.

        Args:
            context: The read-only state snapshot for this tick.

        Returns:
            ``True`` when the condition is met, else ``False``.
        """
        try:
            if self.condition is not None:
                fn = _lookup_condition(self.condition)
                if fn is None:
                    logger.warning(
                        "trigger %r references unknown condition %r; treating as false",
                        self.name,
                        self.condition,
                    )
                    return False
                return bool(fn(context))

            if self.metric is not None:
                if self.metric not in context:
                    # Absent metric never fires (fail-safe).
                    return False
                op_fn = _OPS.get(str(self.op))
                if op_fn is None:
                    logger.warning(
                        "trigger %r has unknown op %r; treating as false",
                        self.name,
                        self.op,
                    )
                    return False
                return bool(op_fn(context[self.metric], self.value))

            logger.warning(
                "trigger %r defines neither 'metric' nor 'condition'; treating as false",
                self.name,
            )
            return False
        except Exception as exc:  # noqa: BLE001 - a rule must never crash the loop
            logger.error("trigger %r evaluation raised: %s", self.name, exc)
            return False

    def in_cooldown(self, now: float) -> bool:
        """Return whether this rule is still inside its cooldown window.

        Args:
            now: Current monotonic timestamp.

        Returns:
            ``True`` if the rule fired less than ``cooldown_seconds`` ago.
        """
        if self.last_fired is None or self.cooldown_seconds <= 0:
            return False
        return (now - self.last_fired) < self.cooldown_seconds


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class TriggerEngine:
    """Evaluates :class:`TriggerRule` rules and fires their actions.

    The engine holds no thread of its own: a caller drives it by calling
    :meth:`tick` with a fresh state snapshot.  Wire it onto the existing
    scheduler via :func:`register_with_scheduler` so it rides the daemon's tick
    rather than spinning up a second loop.

    Idempotent firing is enforced twice over: a per-rule ``cooldown`` (checked
    here) and the notification layer's own content-dedup (in the default sink).

    Args:
        rules: The rules to evaluate, in order.
        action_sink: Callable used to dispatch a ``notify`` action.  Defaults
            to :func:`skcapstone.notifications.notify`.  Injectable so tests can
            capture fires without touching the desktop.
        clock: Monotonic clock used for cooldown math.  Defaults to
            :func:`time.monotonic`.  Injectable so tests control time.
    """

    def __init__(
        self,
        rules: list[TriggerRule],
        action_sink: Optional[ActionSink] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rules = list(rules)
        self._clock = clock
        if action_sink is not None:
            self._action_sink = action_sink
        else:
            # Lazy default: bind at construction to the live notify() sink.
            from .notifications import notify as _notify

            self._action_sink = _notify

    @property
    def rules(self) -> list[TriggerRule]:
        """Return the engine's rules (live list; do not mutate under a tick)."""
        return self._rules

    def tick(self, context: Optional[Context] = None) -> list[str]:
        """Evaluate every enabled rule once and fire those that are due.

        A rule fires when it is enabled, its condition holds for ``context``,
        and it is not inside its cooldown window.  Firing updates the rule's
        ``last_fired`` / ``fire_count`` so a condition that stays true does not
        re-fire until the cooldown lapses.

        Args:
            context: The state snapshot for this tick.  ``None`` is treated as
                an empty snapshot (declarative rules see no metrics and so do
                not fire; named conditions still run against ``{}``).

        Returns:
            The names of the rules that fired this tick, in evaluation order.
        """
        ctx: Context = context or {}
        now = self._clock()
        fired: list[str] = []

        for rule in self._rules:
            if not rule.enabled:
                continue
            if not rule.evaluate(ctx):
                continue
            if rule.in_cooldown(now):
                logger.debug("trigger %r condition met but in cooldown; skipping", rule.name)
                continue
            if self._fire(rule, ctx):
                rule.last_fired = now
                rule.fire_count += 1
                fired.append(rule.name)

        return fired

    def _fire(self, rule: TriggerRule, context: Context) -> bool:
        """Dispatch a rule's action.  Never raises.

        Args:
            rule: The rule whose action to fire.
            context: The tick's state snapshot (available to callback actions).

        Returns:
            ``True`` if the action reports it was dispatched, else ``False``.
        """
        try:
            if rule.action == "notify":
                title = rule.title or rule.name
                # A stable dedup key keyed on the rule name lets the notification
                # layer collapse repeats even across engine restarts within its
                # TTL, on top of our own cooldown.
                return bool(
                    self._action_sink(
                        title,
                        rule.body,
                        rule.urgency,
                        dedup_key=f"trigger:{rule.name}",
                    )
                )
            if rule.action == "callback":
                return self._fire_callback(rule, context)
            logger.warning("trigger %r has unknown action %r; not firing", rule.name, rule.action)
            return False
        except Exception as exc:  # noqa: BLE001 - a fire must never crash the loop
            logger.error("trigger %r action %r raised: %s", rule.name, rule.action, exc)
            return False

    @staticmethod
    def _fire_callback(rule: TriggerRule, context: Context) -> bool:
        """Import and call a ``module:function`` callback action (opt-in).

        The callback is invoked as ``fn(context)`` when it accepts an argument,
        else ``fn()``.  A truthy return (or ``None``) counts as dispatched.

        Args:
            rule: The rule carrying the ``callback`` reference.
            context: The tick snapshot passed to the callback when accepted.

        Returns:
            ``True`` if the callback ran without raising, else ``False``.
        """
        import importlib
        import inspect

        ref = rule.callback or ""
        mod_name, _, fn_name = ref.partition(":")
        if not mod_name or not fn_name:
            logger.error("trigger %r callback %r invalid - expected 'module:fn'", rule.name, ref)
            return False
        module = importlib.import_module(mod_name)
        fn = getattr(module, fn_name)
        try:
            sig = inspect.signature(fn)
            result = fn(context) if len(sig.parameters) >= 1 else fn()
        except (TypeError, ValueError):
            result = fn()
        return result is None or bool(result)


# ---------------------------------------------------------------------------
# Config loading (mirrors scheduler_jobs jobs.yaml / jobs.d convention)
# ---------------------------------------------------------------------------

_KNOWN_KEYS = frozenset(
    {
        "metric",
        "op",
        "value",
        "condition",
        "action",
        "title",
        "body",
        "urgency",
        "callback",
        "cooldown",
        "enabled",
    }
)


def _rule_from_raw(name: str, raw: dict) -> TriggerRule:
    """Build a :class:`TriggerRule` from one raw YAML mapping.

    Args:
        name: The rule name (its YAML key).
        raw: The rule body mapping.

    Returns:
        A populated :class:`TriggerRule`.
    """
    import warnings

    raw = dict(raw or {})
    unknown = set(raw.keys()) - _KNOWN_KEYS
    if unknown:
        warnings.warn(
            f"Trigger {name!r} has unrecognised key(s): {sorted(unknown)}. "
            "Typo in config? Rule may not behave as expected.",
            UserWarning,
            stacklevel=2,
        )

    cooldown_raw = raw.get("cooldown", 300.0)
    cooldown_seconds = _parse_duration(cooldown_raw)

    return TriggerRule(
        name=name,
        metric=raw.get("metric"),
        op=raw.get("op"),
        value=raw.get("value"),
        condition=raw.get("condition"),
        action=str(raw.get("action", "notify")),
        title=raw.get("title"),
        body=str(raw.get("body", "")),
        urgency=str(raw.get("urgency", "normal")),
        callback=raw.get("callback"),
        cooldown_seconds=cooldown_seconds,
        enabled=bool(raw.get("enabled", True)),
    )


def load_triggers(config_path: Path) -> list[TriggerRule]:
    """Load trigger rules from a ``triggers.yaml`` file.

    The file must have a top-level ``triggers`` mapping; each key becomes a
    rule ``name``.  A missing file yields an empty list (no error) so the
    daemon runs cleanly with no rules configured.

    Args:
        config_path: Path to ``triggers.yaml``.

    Returns:
        The rules in definition order (empty if the file is absent).
    """
    if not config_path.exists():
        return []

    import yaml  # lazy - pyyaml optional at module level

    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    raw_rules: dict = (data or {}).get("triggers") or {}
    return [_rule_from_raw(name, raw) for name, raw in raw_rules.items()]


def load_triggers_with_dropins(config_path: Path) -> list[TriggerRule]:
    """Load ``triggers.yaml`` plus every ``triggers.d/*.yaml`` drop-in.

    Mirrors :func:`skcapstone.scheduler_jobs.load_jobs_with_dropins`: the base
    file loads first, then each ``triggers.d/<name>.yaml`` (sorted) overlays it.
    A later definition of the same rule name wins and warns.

    Args:
        config_path: Path to the base ``triggers.yaml``.  Neither it nor the
            drop-in dir need exist.

    Returns:
        The merged rule list, base rules first then drop-in-only rules.
    """
    import warnings

    merged: dict[str, TriggerRule] = {}
    for rule in load_triggers(config_path):
        merged[rule.name] = rule

    dropin_dir = config_path.parent / "triggers.d"
    if dropin_dir.is_dir():
        for fragment in sorted(dropin_dir.glob("*.yaml")):
            for rule in load_triggers(fragment):
                if rule.name in merged:
                    warnings.warn(
                        f"Trigger {rule.name!r} in drop-in {fragment.name!r} "
                        f"overrides an earlier definition.",
                        UserWarning,
                        stacklevel=2,
                    )
                merged[rule.name] = rule

    return list(merged.values())


def default_config_path(home: Optional[Path] = None) -> Path:
    """Return the canonical ``config/triggers.yaml`` path under the SK root.

    Args:
        home: skcapstone root.  Defaults to the shared root (honouring
            ``SKCAPSTONE_HOME``), else ``~/.skcapstone``.

    Returns:
        Path to ``<root>/config/triggers.yaml``.
    """
    if home is not None:
        base = Path(home)
    else:
        try:
            from . import shared_home

            base = shared_home()
        except Exception:
            base = Path("~/.skcapstone").expanduser()
    return base / "config" / "triggers.yaml"


# ---------------------------------------------------------------------------
# Scheduler integration - ride the existing daemon tick, no second thread
# ---------------------------------------------------------------------------


def register_with_scheduler(
    scheduler: Any,
    engine: TriggerEngine,
    context_provider: Callable[[], Context],
    interval_seconds: float = 30.0,
    name: str = "proactive-triggers",
) -> None:
    """Register the engine's tick as a recurring scheduler task.

    This is the seam that makes the trigger engine a *daemon* without a second
    background thread: the framework's :class:`~skcapstone.scheduled_tasks.TaskScheduler`
    already owns one, so we register a task that, every ``interval_seconds``,
    snapshots state via ``context_provider`` and drives :meth:`TriggerEngine.tick`.

    Args:
        scheduler: A ``TaskScheduler`` (anything with a compatible ``register``).
        engine: The engine to drive.
        context_provider: Zero-arg callable returning the current state snapshot.
            Kept conservative and read-only by convention.
        interval_seconds: Evaluation cadence (default 30s).
        name: Scheduler task name.
    """

    def _tick() -> None:
        try:
            context = context_provider()
        except Exception as exc:  # noqa: BLE001 - provider must not crash the scheduler
            logger.error("trigger context_provider raised: %s", exc)
            return
        fired = engine.tick(context)
        if fired:
            logger.info("proactive triggers fired: %s", ", ".join(fired))

    scheduler.register(name, interval_seconds, _tick)


# ---------------------------------------------------------------------------
# Built-in read-only context provider - best-effort host metrics
# ---------------------------------------------------------------------------


def system_metrics_context() -> Context:
    """Return a read-only snapshot of a few host metrics for trigger rules.

    Emits the metric keys the shipped ``triggers.yaml.example`` references:

    * ``disk_free_pct`` - free space on ``/`` as a percentage.
    * ``disk_free_gb`` - free space on ``/`` in GiB.
    * ``mem_available_pct`` - available RAM percentage (Linux ``/proc/meminfo``).
    * ``load_avg_1m`` - 1-minute load average.
    * ``cpu_count`` - logical CPU count (handy as a load denominator in rules).

    Every probe is best-effort and independently guarded: a metric that cannot
    be read is simply omitted, so a rule that references it fails safe (does not
    fire) rather than crashing the tick.  The function performs no writes.

    Returns:
        A metric-name -> value snapshot (only successfully probed keys present).
    """
    import shutil

    ctx: Context = {}

    try:
        usage = shutil.disk_usage("/")
        ctx["disk_free_pct"] = round(usage.free / usage.total * 100.0, 2)
        ctx["disk_free_gb"] = round(usage.free / (1024**3), 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("disk metric probe failed: %s", exc)

    try:
        meminfo: dict[str, float] = {}
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                meminfo[key.strip()] = float(rest.strip().split()[0])  # kB
        total = meminfo.get("MemTotal")
        available = meminfo.get("MemAvailable")
        if total and available is not None:
            ctx["mem_available_pct"] = round(available / total * 100.0, 2)
    except Exception as exc:  # noqa: BLE001 - non-Linux / missing /proc
        logger.debug("mem metric probe failed: %s", exc)

    try:
        ctx["load_avg_1m"] = round(os.getloadavg()[0], 2)
    except Exception as exc:  # noqa: BLE001
        logger.debug("loadavg probe failed: %s", exc)

    try:
        ctx["cpu_count"] = os.cpu_count() or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("cpu_count probe failed: %s", exc)

    return ctx


# ---------------------------------------------------------------------------
# Standalone runner - reuses TaskScheduler (no bespoke loop)
# ---------------------------------------------------------------------------


def run_standalone(
    config_path: Optional[Path] = None,
    context_provider: Callable[[], Context] = system_metrics_context,
    interval_seconds: float = 30.0,
    home: Optional[Path] = None,
) -> None:
    """Run the trigger engine as a standalone daemon and block until signalled.

    This exists for operators who want a triggers-only service.  It does **not**
    spin up a bespoke loop: it reuses :class:`~skcapstone.scheduled_tasks.TaskScheduler`
    (the same machinery the main daemon uses) to own the thread and the tick.
    In-process with the main daemon, prefer :func:`register_with_scheduler`
    instead so there is a single scheduler.

    Blocks the calling thread until ``SIGINT``/``SIGTERM``, then stops cleanly.

    Args:
        config_path: Path to ``triggers.yaml``.  Defaults to
            :func:`default_config_path`.
        context_provider: Zero-arg read-only state snapshot source.  Defaults to
            :func:`system_metrics_context`.
        interval_seconds: Evaluation cadence.
        home: Agent home passed to the scheduler (defaults to ``~/.skcapstone``).
    """
    import signal
    import threading

    from .scheduled_tasks import TaskScheduler

    cfg = config_path or default_config_path()
    rules = load_triggers_with_dropins(cfg)
    logger.info("proactive trigger daemon: loaded %d rule(s) from %s", len(rules), cfg)

    engine = TriggerEngine(rules)
    stop_event = threading.Event()
    scheduler = TaskScheduler(
        home=Path(home) if home is not None else Path("~/.skcapstone").expanduser(),
        stop_event=stop_event,
    )
    register_with_scheduler(scheduler, engine, context_provider, interval_seconds=interval_seconds)

    def _handle_signal(signum, _frame) -> None:
        logger.info("proactive trigger daemon: signal %s, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    scheduler.start()
    stop_event.wait()
    scheduler.stop()


def main() -> None:
    """CLI entrypoint: ``python -m skcapstone.triggers``."""
    logging.basicConfig(
        level=os.environ.get("SKCAPSTONE_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_standalone()


if __name__ == "__main__":  # pragma: no cover
    main()
