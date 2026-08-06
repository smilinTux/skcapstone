"""skdashboard: the SKWorld operator dashboard (coord board + ITIL + kanban + CMDB).

Extracted from ``skcapstone`` (CR-4.3). Serves the ``:7778`` web UI + JSON API over
the coordination cluster. Coordination access goes through ``skcoord`` directly;
the richer agent/runtime/trust/model panels reach back into ``skcapstone`` at
runtime (lazy imports), so ``skdashboard`` depends on both but has no import-time
cycle (the dashboard is launched on demand, after skcapstone is already up).

Public entry points (imported lazily to keep package import cheap):
``start_dashboard`` and ``create_app`` live in ``skdashboard.dashboard``.
"""
from __future__ import annotations

__version__ = "0.1.0"


def __getattr__(name):
    # Lazy passthrough so `from skdashboard import start_dashboard` works without
    # importing the (heavy) dashboard module at package load.
    if name in {"start_dashboard", "create_app"}:
        from . import dashboard as _d

        return getattr(_d, name)
    raise AttributeError(f"module 'skdashboard' has no attribute {name!r}")
