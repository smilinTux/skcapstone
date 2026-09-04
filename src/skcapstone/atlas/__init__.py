"""Atlas read-only estate views.

Everything in this package is pure observation. Atlas runs under
``docs/ATLAS_CONSTITUTION.md``, whose Article 1 already describes the posture
this package is permanently pinned to: "Atlas observes and reports but takes no
action". Nothing here writes a file, issues an actuation verb, or reads the
coordination board, and ``tests/test_atlas_reconcile.py`` walks this package's
AST to keep that true rather than merely stated.
"""

from __future__ import annotations
