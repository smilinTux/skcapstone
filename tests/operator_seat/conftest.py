"""Shared isolation for the operator seat tests.

Round 6 of the gate 5 hardening moved two things out of the synced fleet tree and behind
node-local configuration: the per-change locks (a node-local state directory) and the
single-actuator-node rule (actuation refuses unless this node is the designated actuator,
default NONE). Without help, every test that actuates would write into the real home
directory and be refused. So each test gets its own node-local state directory and the
designation gate is opened; the tests that exercise the rule itself patch it back.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _node_local_isolation(monkeypatch, tmp_path):
    monkeypatch.setenv("SKCAPSTONE_LOCAL_STATE_DIR", str(tmp_path / "node-local-state"))
    try:
        node_local = importlib.import_module("skcapstone.operator_seat.node_local")
    except ImportError:  # the module does not exist yet (pre round 6 source)
        return
    monkeypatch.setattr(node_local, "actuator_refusal", lambda: None)
