"""P1 wiring tests: skcapstone.agent_run._maybe_wire_execute_bridge().

Design doc: docs/specs/2026-08-13-skharness-execute-bridge-arch.md section 6.
Proves every path stays fail-closed except the fully-satisfied one, and that
nothing here imports skharness at module import time (no wiring happens
unless SKAI_EXECUTE_BRIDGE=1 is explicitly set).

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import sys
import types

import pytest

from skcapstone import agent_run as ar

_BRIDGE_MODULE = "skharness.autocode.agentrun_bridge"


@pytest.fixture(autouse=True)
def _reset_execute_dispatcher():
    """The seam is a module global; a leaked dispatcher would poison
    unrelated tests."""
    ar.set_execute_dispatcher(None)
    yield
    ar.set_execute_dispatcher(None)


def _install_fake_bridge_module(monkeypatch, factory):
    """Insert a fake ``skharness.autocode.agentrun_bridge`` module into
    sys.modules so ``from skharness.autocode.agentrun_bridge import
    build_execute_dispatcher`` resolves to ``factory`` without needing a real
    skharness install. Restored automatically by monkeypatch teardown."""
    fake = types.ModuleType(_BRIDGE_MODULE)
    fake.build_execute_dispatcher = factory
    monkeypatch.setitem(sys.modules, _BRIDGE_MODULE, fake)


# --------------------------------------------------------------------------- #
# (a) SKAI_EXECUTE_BRIDGE unset -> no-op                                      #
# --------------------------------------------------------------------------- #


def test_flag_unset_is_a_noop(monkeypatch):
    monkeypatch.delenv("SKAI_EXECUTE_BRIDGE", raising=False)
    ar._maybe_wire_execute_bridge()
    assert ar.execute_dispatch_available() is False


# --------------------------------------------------------------------------- #
# (b) skharness not installed (ImportError) -> fail-closed                    #
# --------------------------------------------------------------------------- #


def test_import_error_stays_fail_closed(monkeypatch, caplog):
    monkeypatch.setenv("SKAI_EXECUTE_BRIDGE", "1")
    # sys.modules[name] = None forces the import statement to raise ImportError
    # regardless of whether skharness is actually installed in this venv.
    monkeypatch.setitem(sys.modules, _BRIDGE_MODULE, None)
    with caplog.at_level("INFO", logger="skcapstone.agent_run"):
        ar._maybe_wire_execute_bridge()
    assert ar.execute_dispatch_available() is False
    assert any("not installed" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# (c) factory returns None -> fail-closed                                     #
# --------------------------------------------------------------------------- #


def test_factory_returns_none_stays_fail_closed(monkeypatch, caplog):
    monkeypatch.setenv("SKAI_EXECUTE_BRIDGE", "1")
    _install_fake_bridge_module(monkeypatch, factory=lambda: None)
    with caplog.at_level("INFO", logger="skcapstone.agent_run"):
        ar._maybe_wire_execute_bridge()
    assert ar.execute_dispatch_available() is False
    assert any("prerequisites missing" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# (d) factory returns a callable -> wired                                     #
# --------------------------------------------------------------------------- #


def test_factory_returns_dispatcher_wires_it(monkeypatch):
    monkeypatch.setenv("SKAI_EXECUTE_BRIDGE", "1")
    sentinel = lambda ctx: {"summary": "ok", "activity": [], "links": {}}  # noqa: E731
    _install_fake_bridge_module(monkeypatch, factory=lambda: sentinel)

    ar._maybe_wire_execute_bridge()

    assert ar.execute_dispatch_available() is True
    assert ar._execute_dispatcher is sentinel


def test_already_wired_is_a_noop_and_does_not_reimport(monkeypatch):
    """If something already wired a dispatcher, the helper must not clobber
    it by re-running the factory (idempotent, cheap to call every job tick)."""
    monkeypatch.setenv("SKAI_EXECUTE_BRIDGE", "1")
    existing = lambda ctx: {"summary": "existing", "activity": [], "links": {}}  # noqa: E731
    ar.set_execute_dispatcher(existing)

    def _blow_up():
        raise AssertionError("factory must not be called when already wired")

    _install_fake_bridge_module(monkeypatch, factory=_blow_up)

    ar._maybe_wire_execute_bridge()

    assert ar._execute_dispatcher is existing
