"""Re-export shim: dashboard_kanban moved to skdashboard (CR-4.3 dashboard split).

``skcapstone.dashboard_kanban`` is now a transparent alias of the skdashboard module
(same object via sys.modules), so the ``skcapstone dashboard`` CLI, the inbound
sites in _cli_monolith, and the dashboard tests all resolve to the real
implementation byte-identically. New code should import from ``skdashboard``.
"""

from __future__ import annotations

import sys

import skdashboard.dashboard_kanban as _src

sys.modules[__name__] = _src
