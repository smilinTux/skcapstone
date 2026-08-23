"""Re-export shim: the event-sourced CardStore moved to skcoord (CR-4.1 extraction).

``skcapstone.card_store`` is now a transparent alias of ``skcoord.card_store`` (the same
module object via sys.modules), so every importer, attribute access, and
``monkeypatch.setattr`` on a class OR a module global reaches the real
implementation byte-identically. New code should import from ``skcoord`` directly.
"""

from __future__ import annotations

import sys

import skcoord.card_store as _src

sys.modules[__name__] = _src
