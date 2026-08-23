"""Operator seat: the cognitive layer that proposes and applies fleet changes.

Seat O1 splits into a pure policy half (change class / risk classification,
see :mod:`skcapstone.operator_seat.policy`) and a wired half that reads live
fleet state and actuates through skfleet. Only the pure half ships here.
"""

from __future__ import annotations
