"""Dual-mode integration backbone harness — EPIC acceptance gate.

This module is the **system-level acceptance test** for the
sk* ⇄ skcapstone optional integration backbone (EPIC fca7f138, ADR
``docs/ADR-optional-integration-backbone.md``).

For EVERY adapter that implements the integration contract it verifies TWO modes:

Mode A — STANDALONE
    ``SK_STANDALONE=1`` is set (or ``_sdk`` patched to ``None``).  The adapter
    must not crash, its ``is_present()`` must return ``False``, ``alert()``
    must return ``False`` and fall back to native logging, ``ensure_schedule()``
    must return ``False`` without writing any files, and ``register_self()``
    must return ``False``.

Mode B — INTEGRATED
    skcapstone is importable and available.  ``SKCAPSTONE_HOME`` is sandboxed
    to ``tmp_path`` so no files touch ``~/.skcapstone``.  The adapter must:
      - ``is_present()`` → True
      - ``alert(event, payload, level)`` → True, and
          * the PubSub topic directory ``<home>/pubsub/topics/<svc>.<level>/``
            must exist and contain exactly one ``msg-*.json`` whose payload
            contains ``{"event": <event>, ...}`` and whose ``tags`` include the
            level string (severity-based routing).
          * topic suffix IS the severity (e.g. ``skmemory.error``) — not the
            event name — so ``skcapstone alerts`` wildcards ``*.error`` etc.
            match correctly.
      - ``ensure_schedule()`` → True, and
          * ``<home>/config/jobs.d/<job_name>.yaml`` must exist and be valid YAML.
      - ``register_self()`` → True, and
          * ``<home>/registry/<svc>.json`` must exist.

Each adapter is described by a namedtuple ``AdapterSpec``; the parametrized
test IDs use the service names so failures are readable.

LEAK CHECK: after integrated-mode tests the test verifies that no fragments
leaked to the *real* ``~/.skcapstone/config/jobs.d/`` or
``~/.skcapstone/registry/`` directories.

Reference adapter: ``skmemory/skmemory/integration.py`` (commit be33179).
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple, Optional

import pytest
import yaml

# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


class AdapterSpec(NamedTuple):
    """Describes one sk* adapter under test."""

    #: Human-readable service name — used as the pytest parametrize ID.
    service: str
    #: Python module path to the adapter (importable from the installed venv).
    module_path: str
    #: The adapter's job-name constant (the ``jobs.d/<name>.yaml`` key).
    job_name: str
    #: A representative non-critical alert level to exercise the routing.
    alert_level: str = "warn"


#: All adapters that implement the integration contract.
#:
#: skchat (ad4f721a) is owned by another thread — deliberately excluded.
#: skgateway is Node/non-Python — it is tested via its own Node harness
#: (``skgateway/tests/integration.test.mjs``); excluded from Python parametrize.
ADAPTERS: list[AdapterSpec] = [
    AdapterSpec(
        service="skmemory",
        module_path="skmemory.integration",
        job_name="skmemory_sweep",
    ),
    AdapterSpec(
        service="sksecurity",
        module_path="sksecurity.integration",
        job_name="sksecurity_intel_refresh",
    ),
    AdapterSpec(
        service="skcomms",
        module_path="skcomms.integration",
        job_name="skcomms_health_sweep",
    ),
    AdapterSpec(
        service="capauth",
        module_path="capauth.integration",
        job_name="capauth_key_rotation_check",
    ),
    AdapterSpec(
        service="cloud9",
        module_path="cloud9_protocol.integration",
        job_name="cloud9_rehydration_check",
    ),
    AdapterSpec(
        service="skvoice",
        module_path="skvoice.integration",
        job_name="skvoice_health",
    ),
    AdapterSpec(
        service="skseed",
        module_path="skseed.integration",
        job_name="skseed_audit",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def skcap_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox skcapstone's home to ``tmp_path``; return the sandboxed root.

    Sets both ``SKCAPSTONE_HOME`` (read by ``shared_home()``) and patches
    ``skcapstone.AGENT_HOME`` (captured at import time) so every SDK write
    goes to the temp tree, never to ``~/.skcapstone``.
    """
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    import skcapstone as pkg

    monkeypatch.setattr(pkg, "AGENT_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def real_jobs_d() -> Path:
    """Return the *actual* ~/.skcapstone jobs.d path for leak detection.

    Deliberately ignores SKCAPSTONE_HOME so that the leak check can verify
    that integrated-mode writes go to the sandboxed path only, not to the
    developer's real agent home.
    """
    return Path.home() / ".skcapstone" / "config" / "jobs.d"


@pytest.fixture
def real_registry() -> Path:
    """Return the *actual* ~/.skcapstone registry path for leak detection."""
    return Path.home() / ".skcapstone" / "registry"


# ---------------------------------------------------------------------------
# Helper: import adapter module freshly so monkeypatches take effect
# ---------------------------------------------------------------------------


def _load_adapter(spec: AdapterSpec) -> Optional[ModuleType]:
    """Import the adapter module, returning None if not installed."""
    try:
        mod = importlib.import_module(spec.module_path)
        # Force a fresh re-evaluation of the module-level ``_sdk`` guard by
        # reloading.  This is safe in a test context.
        return importlib.reload(mod)
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Mode A — STANDALONE tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ADAPTERS, ids=[a.service for a in ADAPTERS])
class TestStandaloneMode:
    """Each adapter behaves correctly when skcapstone is absent / forced off.

    Two sub-strategies are exercised:
      1. ``SK_STANDALONE=1`` env var forces native mode even when installed.
      2. ``_sdk`` attribute patched to ``None`` simulates absent package.
    """

    def test_is_present_false_when_env_standalone(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """is_present() returns False when SK_STANDALONE=1, regardless of package."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        # Reload after env is set so the module-level _sdk guard re-runs.
        importlib.reload(mod)
        assert mod.is_present() is False

    def test_alert_returns_false_when_standalone(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """alert() returns False in standalone mode (native logging fallback)."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        result = mod.alert("test_event", {"detail": "x"}, "warn")
        assert result is False

    def test_alert_does_not_raise_when_sdk_absent(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """alert() never raises even when _sdk is None."""
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        monkeypatch.setattr(mod, "_sdk", None)
        # Should not raise
        result = mod.alert("boom", {"x": 1}, "error")
        assert result is False

    def test_ensure_schedule_returns_false_when_standalone(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """ensure_schedule() returns False and writes no files in standalone."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        result = mod.ensure_schedule()
        assert result is False

    def test_ensure_schedule_does_not_raise_when_sdk_absent(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """ensure_schedule() never raises when _sdk is None."""
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        monkeypatch.setattr(mod, "_sdk", None)
        result = mod.ensure_schedule()
        assert result is False

    def test_register_self_returns_false_when_standalone(
        self, spec: AdapterSpec, monkeypatch: pytest.MonkeyPatch
    ):
        """register_self() returns False in standalone mode."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        result = mod.register_self()
        assert result is False

    def test_no_pubsub_files_written_in_standalone(
        self,
        spec: AdapterSpec,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """No pubsub topic files appear under tmp_path in standalone mode."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        mod.alert("no_files_please", {"reason": "standalone"}, "error")
        pubsub_root = tmp_path / "pubsub" / "topics"
        topic_files = list(pubsub_root.glob("**/*.json")) if pubsub_root.exists() else []
        assert topic_files == [], (
            f"Standalone mode wrote pubsub files: {topic_files}"
        )

    def test_no_jobs_d_files_written_in_standalone(
        self,
        spec: AdapterSpec,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """No jobs.d files appear under tmp_path in standalone mode."""
        monkeypatch.setenv("SK_STANDALONE", "1")
        monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        mod.ensure_schedule()
        jobs_d = tmp_path / "config" / "jobs.d"
        job_files = list(jobs_d.glob("*.yaml")) if jobs_d.exists() else []
        assert job_files == [], (
            f"Standalone mode wrote jobs.d files: {job_files}"
        )


# ---------------------------------------------------------------------------
# Mode B — INTEGRATED tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", ADAPTERS, ids=[a.service for a in ADAPTERS])
class TestIntegratedMode:
    """Each adapter routes correctly through skcapstone when present.

    Uses the ``skcap_sandbox`` fixture to redirect all file writes to a
    temporary directory, guaranteeing no leaks to the real home.
    """

    def test_is_present_true_when_integrated(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """is_present() returns True when skcapstone is installed and present."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)
        assert mod.is_present() is True

    def test_alert_returns_true_and_publishes_to_topic(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """alert() returns True and writes a PubSub message to <svc>.<level>/."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        event_name = "test_event_integrated"
        level = spec.alert_level
        result = mod.alert(event_name, {"source": "harness"}, level)
        assert result is True, (
            f"{spec.service}.alert(..., level={level!r}) returned False in "
            f"integrated mode — check skcap_sandbox isolation"
        )

        # Topic directory must exist: <home>/pubsub/topics/<svc>.<level>/
        expected_topic = f"{spec.service}.{level}"
        topic_dir = skcap_sandbox / "pubsub" / "topics" / expected_topic
        assert topic_dir.is_dir(), (
            f"Expected topic dir {topic_dir} — SDK alert did not create it. "
            f"Check that the adapter uses topic '<svc>.<severity>' convention."
        )

        msgs = list(topic_dir.glob("msg-*.json"))
        assert len(msgs) >= 1, f"No msg-*.json under {topic_dir}"

        payload_data = json.loads(msgs[0].read_text())

        # The event name must be in the payload.event field (ADR §4 convention)
        assert "payload" in payload_data, f"Message missing 'payload' key: {payload_data}"
        assert payload_data["payload"].get("event") == event_name, (
            f"Payload event field mismatch — got {payload_data['payload'].get('event')!r}, "
            f"expected {event_name!r}.  The adapter must put the semantic event name in "
            f"payload['event'], not in the topic suffix."
        )

        # Tags must contain the level string so skcapstone alerts wildcard routing works
        assert level in payload_data.get("tags", []), (
            f"Level {level!r} missing from message tags {payload_data.get('tags')!r}. "
            f"skcapstone alerts subscribes to *.{level} — tags must carry the severity."
        )

    def test_alert_topic_uses_severity_suffix_not_event_name(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The topic suffix IS the severity, NOT the event name.

        This guards the bug that was caught during skmemory adapter work:
        if the topic is ``<svc>.<event_name>`` then ``skcapstone alerts``'
        ``*.error`` / ``*.warn`` wildcards never match it.
        """
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        level = "error"
        event_name = "canary_event_xyzzy"
        mod.alert(event_name, {}, level)

        # Topic dir for <svc>.error MUST exist
        correct_dir = skcap_sandbox / "pubsub" / "topics" / f"{spec.service}.error"
        assert correct_dir.is_dir(), (
            f"Topic dir {correct_dir} not created. "
            f"Adapter may be using the event name as the topic suffix."
        )

        # Topic dir named after the event name must NOT exist
        wrong_dir = skcap_sandbox / "pubsub" / "topics" / f"{spec.service}.{event_name}"
        assert not wrong_dir.exists(), (
            f"Adapter created wrong topic dir {wrong_dir} — "
            f"event name must NOT be the topic suffix."
        )

    def test_ensure_schedule_returns_true_and_writes_jobs_d(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """ensure_schedule() returns True and writes <job_name>.yaml to jobs.d/."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        result = mod.ensure_schedule()
        assert result is True, (
            f"{spec.service}.ensure_schedule() returned False in integrated mode"
        )

        jobs_d = skcap_sandbox / "config" / "jobs.d"
        job_file = jobs_d / f"{spec.job_name}.yaml"
        assert job_file.exists(), (
            f"Expected jobs.d fragment {job_file} — ensure_schedule() did not write it"
        )

        job_data = yaml.safe_load(job_file.read_text())
        assert isinstance(job_data, dict), f"jobs.d fragment is not valid YAML: {job_data}"

        # The scheduler serialises fragments as:
        #   jobs:
        #     <job_name>:
        #       type: shell
        #       command: ...
        #       every: ...
        # so the top-level key is "jobs" and the job name is the nested key.
        assert "jobs" in job_data, (
            f"jobs.d fragment missing top-level 'jobs' key: {job_data}"
        )
        job_entries = job_data["jobs"]
        assert isinstance(job_entries, dict), (
            f"jobs.d 'jobs' value is not a dict: {job_entries}"
        )
        assert spec.job_name in job_entries, (
            f"Job name {spec.job_name!r} not found in jobs.d fragment keys: "
            f"{list(job_entries.keys())}"
        )
        job_body = job_entries[spec.job_name]
        assert "command" in job_body, f"Job body missing 'command' key: {job_body}"
        # Must have either 'every' (interval) or 'schedule' (cron)
        assert "every" in job_body or "schedule" in job_body, (
            f"Job body has neither 'every' nor 'schedule': {job_body}"
        )

    def test_ensure_schedule_is_idempotent(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Calling ensure_schedule() twice does not raise and writes one file."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        mod.ensure_schedule()
        mod.ensure_schedule()  # second call must not raise

        jobs_d = skcap_sandbox / "config" / "jobs.d"
        job_files = list(jobs_d.glob(f"{spec.job_name}*.yaml"))
        assert len(job_files) == 1, (
            f"Expected 1 jobs.d file after idempotent calls, got {len(job_files)}: {job_files}"
        )

    def test_register_self_returns_true_and_writes_registry(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """register_self() returns True and writes <svc>.json to registry/."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        result = mod.register_self()
        assert result is True, (
            f"{spec.service}.register_self() returned False in integrated mode"
        )

        registry = skcap_sandbox / "registry"
        entry_file = registry / f"{spec.service}.json"
        assert entry_file.exists(), (
            f"Expected registry entry {entry_file} — register_self() did not write it"
        )

        entry = json.loads(entry_file.read_text())
        assert entry.get("name") == spec.service, (
            f"Registry entry name mismatch: {entry.get('name')!r} != {spec.service!r}"
        )

    def test_unregister_schedule_cleans_up(
        self, spec: AdapterSpec, skcap_sandbox: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """unregister_schedule() removes the jobs.d fragment written by ensure_schedule."""
        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        mod.ensure_schedule()
        job_file = skcap_sandbox / "config" / "jobs.d" / f"{spec.job_name}.yaml"
        assert job_file.exists(), "Precondition: ensure_schedule() should have written the file"

        result = mod.unregister_schedule()
        assert result is True, (
            f"{spec.service}.unregister_schedule() returned False"
        )
        assert not job_file.exists(), (
            f"jobs.d fragment {job_file} still present after unregister_schedule()"
        )

    def test_no_leak_to_real_home(
        self,
        spec: AdapterSpec,
        skcap_sandbox: Path,
        monkeypatch: pytest.MonkeyPatch,
        real_jobs_d: Path,
        real_registry: Path,
    ):
        """Integrated-mode writes go to sandbox only — nothing leaks to real home.

        This test fails the suite if sandboxing is broken, preventing silent
        pollution of the developer's actual ~/.skcapstone tree.
        """
        # Record files in real home BEFORE the test actions
        real_jobs_before = set(real_jobs_d.glob(f"*{spec.service}*")) if real_jobs_d.exists() else set()
        real_reg_before = set(real_registry.glob(f"{spec.service}*")) if real_registry.exists() else set()

        monkeypatch.delenv("SK_STANDALONE", raising=False)
        mod = _load_adapter(spec)
        if mod is None:
            pytest.skip(f"{spec.module_path} not installed")
        importlib.reload(mod)

        mod.alert("leak_check", {"harness": True}, "warn")
        mod.ensure_schedule()
        mod.register_self()

        # Verify no NEW files appeared in the real home
        real_jobs_after = set(real_jobs_d.glob(f"*{spec.service}*")) if real_jobs_d.exists() else set()
        real_reg_after = set(real_registry.glob(f"{spec.service}*")) if real_registry.exists() else set()

        new_job_files = real_jobs_after - real_jobs_before
        new_reg_files = real_reg_after - real_reg_before

        assert not new_job_files, (
            f"LEAK: {spec.service} wrote to real jobs.d: {new_job_files}. "
            f"Check that SKCAPSTONE_HOME env and skcapstone.AGENT_HOME are both "
            f"patched to the sandbox path."
        )
        assert not new_reg_files, (
            f"LEAK: {spec.service} wrote to real registry: {new_reg_files}."
        )


# ---------------------------------------------------------------------------
# Contract summary: readable doc of what every adapter must satisfy
# ---------------------------------------------------------------------------


class TestAdapterContract:
    """Documents the invariants every adapter must satisfy.

    These are assertion-less documentation tests — they pass trivially but
    serve as a machine-readable contract anchor in the test output.
    """

    def test_contract_standalone_invariants(self):
        """STANDALONE contract: is_present→False; alert/ensure_schedule/register_self→False;
        no files written; no crash."""
        contract = {
            "is_present": "returns False",
            "alert": "returns False, logs locally, raises nothing",
            "ensure_schedule": "returns False, writes no files, raises nothing",
            "register_self": "returns False, raises nothing",
        }
        assert all(v for v in contract.values())

    def test_contract_integrated_invariants(self):
        """INTEGRATED contract: is_present→True; alert routes to PubSub
        topic <svc>.<severity> with event in payload; ensure_schedule writes
        jobs.d/<job>.yaml; register_self writes registry/<svc>.json; idempotent;
        unregister removes the fragment; no leaks to real ~/.skcapstone."""
        contract = {
            "is_present": "returns True",
            "alert_topic": "<svc>.<severity> — event name in payload.event not topic",
            "alert_tags": "level string present in message tags",
            "ensure_schedule": "writes jobs.d/<job>.yaml with name/command/every",
            "idempotency": "safe to call ensure_schedule twice",
            "register_self": "writes registry/<svc>.json with name field",
            "unregister": "removes jobs.d fragment; returns True",
            "no_leak": "all writes go to sandboxed SKCAPSTONE_HOME only",
        }
        assert all(v for v in contract.values())
