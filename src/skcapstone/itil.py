"""Re-export shim: ITIL service management moved to skcoord (CR-4.1 extraction).

``skcapstone.itil`` is now a transparent alias of ``skcoord.itil`` (the same
module object via sys.modules), so every importer, attribute access, and
``monkeypatch.setattr`` on a class OR a module global reaches the real
implementation byte-identically. New code should import from ``skcoord`` directly.
"""

from __future__ import annotations

import sys

import skcoord.itil as _src

sys.modules[__name__] = _src
