"""Re-export shim: the atomic file-write helper moved to skcoord (CR-4.1 extraction).

``skcapstone.atomic_io`` is now a transparent alias of ``skcoord.atomic_io`` (the same
module object via sys.modules), so every importer, attribute access, and
``monkeypatch.setattr`` on a class OR a module global reaches the real
implementation byte-identically. New code should import from ``skcoord`` directly.
"""

from __future__ import annotations

import sys

import skcoord.atomic_io as _src

sys.modules[__name__] = _src
