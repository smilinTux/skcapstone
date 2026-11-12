"""Doctor checks for the Syncthing ignore standard, and the include-aware readers.

SYNCTHING_IGNORE_STANDARD: a synced ``.stignore-shared`` holds the rules and every
node's ``.stignore`` is a shim that includes it. Each doctor finding has a test, and
every test runs with HOME pointed at ``tmp_path``: the real ``~/.skcapstone`` and the
real Syncthing config are never read.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skcapstone.doctor import _check_stignore, _check_sync_conflicts
from skcapstone.stignore_layout import (
    CRITICAL_RULES,
    diagnose,
    fetch_effective_rules,
    install,
    packaged_shared_text,
    packaged_shim_text,
    read_effective_text,
)

WORKER = """\
// worker allowlist
*.key
*.pem
**/private.*
*.env
config/*.env
(?d)**/*.lock
!/fleet
!/fleet/**
!/config/**
**
"""


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """HOME is a throwaway; no Syncthing endpoint is configured unless a test sets one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in ("SYNCTHING_API_URL", "SYNCTHING_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def root(tmp_path) -> Path:
    path = tmp_path / "home" / ".skcapstone"
    path.mkdir()
    return path


def _by_name(checks):
    return {c.name: c for c in checks}


def _without(root: Path, rule: str) -> None:
    shared = root / ".stignore-shared"
    kept = [ln for ln in shared.read_text().splitlines() if ln.strip() != rule]
    shared.write_text("\n".join(kept) + "\n")


class TestConformingNode:
    def test_a_fresh_install_passes_every_check(self, root):
        install(root)
        checks = _check_stignore(root)
        assert [c.name for c in checks] == [
            "sync:stignore-shim",
            "sync:stignore-shared",
            "sync:stignore-shared-drift",
            "sync:stignore-critical-rules",
        ]
        assert all(c.passed for c in checks), [(c.name, c.detail) for c in checks if not c.passed]

    def test_a_missing_root_yields_no_checks(self, tmp_path):
        assert _check_stignore(tmp_path / "nope") == []

    def test_node_local_rules_below_the_include_are_fine(self, root):
        install(root)
        with (root / ".stignore").open("a") as fh:
            fh.write("scratch-disk\n")
        assert all(c.passed for c in _check_stignore(root))


class TestShimFinding:
    def test_legacy_stignore_is_flagged_and_names_the_migration(self, root):
        (root / ".stignore").write_text("*.key\n*.pem\n**/private.*\n")
        checks = _by_name(_check_stignore(root))
        shim = checks["sync:stignore-shim"]
        assert not shim.passed
        assert "not a shim" in shim.detail
        assert "--migrate" in shim.fix

    def test_missing_stignore_is_flagged(self, root):
        checks = _by_name(_check_stignore(root))
        assert not checks["sync:stignore-shim"].passed
        assert not checks["sync:stignore-critical-rules"].passed

    def test_a_shim_that_lacks_the_include_line_is_not_a_shim(self, root):
        (root / ".stignore").write_text("// a comment that mentions .stignore-shared\n")
        (root / ".stignore-shared").write_text(packaged_shared_text())
        assert not _by_name(_check_stignore(root))["sync:stignore-shim"].passed


class TestSharedFileFindings:
    def test_missing_shared_file_is_flagged(self, root):
        (root / ".stignore").write_text(packaged_shim_text())
        checks = _by_name(_check_stignore(root))
        assert not checks["sync:stignore-shared"].passed
        assert "errors the folder" in checks["sync:stignore-shared"].detail
        # with the include dangling, the effective ruleset is empty: critical rules go too
        assert not checks["sync:stignore-critical-rules"].passed

    def test_drift_from_the_packaged_version_is_reported_not_repaired(self, root):
        install(root)
        shared = root / ".stignore-shared"
        shared.write_text(packaged_shared_text() + "extra-local-rule\n")
        before = shared.read_text()
        checks = _by_name(_check_stignore(root))
        drift = checks["sync:stignore-shared-drift"]
        assert not drift.passed
        assert "1 only here" in drift.detail
        assert "Report only" in drift.fix
        assert shared.read_text() == before, "the doctor never writes"
        assert checks["sync:stignore-critical-rules"].passed, "drift alone is not a leak"


class TestCriticalRules:
    @pytest.mark.parametrize("rule", CRITICAL_RULES)
    def test_each_critical_rule_is_enforced(self, root, rule):
        install(root)
        _without(root, rule)
        crit = _by_name(_check_stignore(root))["sync:stignore-critical-rules"]
        assert not crit.passed
        assert rule in crit.detail

    def test_the_rule_is_found_by_name_not_by_prefix_or_position(self, root):
        install(root)
        _without(root, "*.key")
        (root / ".stignore").write_text(packaged_shim_text() + "(?d)*.key\n")  # flag prefix ok
        assert _by_name(_check_stignore(root))["sync:stignore-critical-rules"].passed

    def test_a_near_miss_does_not_count(self, root):
        install(root)
        _without(root, "*.key")
        (root / ".stignore").write_text(packaged_shim_text() + "*.keys\n")
        assert not _by_name(_check_stignore(root))["sync:stignore-critical-rules"].passed


class TestWorkerAllowlist:
    def test_a_worker_allowlist_is_a_recognised_variant(self, root):
        (root / ".stignore").write_text(WORKER)
        checks = _check_stignore(root)
        assert all(c.passed for c in checks), [(c.name, c.detail) for c in checks if not c.passed]
        assert "worker" in _by_name(checks)["sync:stignore-shim"].detail
        assert diagnose(root).kind == "worker"

    def test_a_worker_missing_a_key_rule_is_flagged(self, root):
        (root / ".stignore").write_text(WORKER.replace("*.env\n", "", 1))
        crit = _by_name(_check_stignore(root))["sync:stignore-critical-rules"]
        assert not crit.passed and "*.env" in crit.detail

    def test_a_key_rule_below_the_allowlist_is_unreachable_and_flagged(self, root):
        """First match wins: a rule after `!/config/**` never sees config/ files."""
        text = WORKER.replace("*.env\n", "", 1).replace("**\n", "*.env\n**\n")
        (root / ".stignore").write_text(text)
        assert not _by_name(_check_stignore(root))["sync:stignore-critical-rules"].passed

    def test_an_allowlist_without_the_catch_all_is_not_a_worker(self, root):
        """`!` rules only mean something when a final `**` closes the allowlist."""
        text = "*.key\n*.pem\n**/private.*\n*.env\n!/fleet\n"
        (root / ".stignore").write_text(text)
        assert diagnose(root).kind == "legacy"
        assert not _by_name(_check_stignore(root))["sync:stignore-shim"].passed


class TestSyncConflicts:
    def test_count_and_paths_are_reported(self, root):
        for name in ("a", "b"):
            d = root / "agents" / name
            d.mkdir(parents=True)
            (d / "x.sync-conflict-20261112-010101-ABCDEFG.json").write_text("{}")
        check = _check_sync_conflicts(root)[0]
        assert not check.passed
        assert "2 conflict file(s)" in check.detail
        assert "agents/a/x.sync-conflict-20261112-010101-ABCDEFG.json" in check.detail
        assert "agents/b/x.sync-conflict-" in check.detail

    def test_the_path_list_is_capped_but_the_count_is_exact(self, root):
        d = root / "logs"
        d.mkdir()
        for i in range(25):
            (d / f"f{i:02d}.sync-conflict-20261112-0101{i:02d}-ABCDEFG.txt").write_text("x")
        check = _check_sync_conflicts(root)[0]
        assert "25 conflict file(s)" in check.detail
        assert "(+15 more)" in check.detail

    def test_report_only_nothing_is_deleted(self, root):
        f = root / "a.sync-conflict-20261112-010101-ABCDEFG.json"
        f.write_text("{}")
        _check_sync_conflicts(root)
        assert f.exists()


# --------------------------------------------------------------- the daemon ---


class _Daemon:
    """A loopback stand-in for Syncthing's ``GET /rest/db/ignores``."""

    def __init__(self, payload, status=200):
        outer = self
        self.requests: list[tuple[str, str | None]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.requests.append((self.path, self.headers.get("X-API-Key")))
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def daemon_for(root, monkeypatch, _isolated_home):
    """Point HOME's Syncthing config at ``root`` and serve the given payload."""
    started: list[_Daemon] = []

    def start(payload, status=200, folder_path=None):
        daemon = _Daemon(payload, status)
        started.append(daemon)
        cfg = _isolated_home / ".config" / "syncthing"
        cfg.mkdir(parents=True, exist_ok=True)
        (cfg / "config.xml").write_text(
            f'<configuration><folder id="estate" path="{folder_path or root}"/></configuration>'
        )
        monkeypatch.setenv("SYNCTHING_API_URL", daemon.url)
        monkeypatch.setenv("SYNCTHING_API_KEY", "test-key")
        return daemon

    yield start
    for daemon in started:
        daemon.close()


def _expanded(rules):
    """Syncthing returns each rule in several expanded forms."""
    out = []
    for rule in rules:
        out += [rule, f"{rule}/**"]
    return out


class TestEffectiveRulesFromTheDaemon:
    def test_the_daemons_expanded_rules_are_used_when_it_answers(self, root, daemon_for):
        install(root)
        daemon = daemon_for({"ignore": [], "expanded": _expanded(CRITICAL_RULES)})
        diag = diagnose(root)
        assert diag.source == "syncthing-api"
        assert "critical_missing" not in diag.codes()
        path, key = daemon.requests[0]
        assert path.startswith("/rest/db/ignores?folder=estate"), "folder id from config.xml"
        assert key == "test-key"

    def test_a_rule_missing_from_the_daemons_view_is_flagged_even_if_the_file_has_it(
        self, root, daemon_for
    ):
        """The daemon's view is what is enforced: a stale daemon is a real gap."""
        install(root)
        rules = [r for r in CRITICAL_RULES if r != "/secrets"]
        daemon_for({"expanded": _expanded(rules)})
        crit = _by_name(_check_stignore(root))["sync:stignore-critical-rules"]
        assert not crit.passed and "/secrets" in crit.detail

    def test_a_daemon_ignore_error_is_reported(self, root, daemon_for):
        install(root)
        daemon_for({"error": "open .stignore-shared: no such file or directory"})
        diag = diagnose(root)
        assert "daemon_ignore_error" in diag.codes()
        assert diag.source == "files", "falls back to the files for the rule check"

    def test_an_unreachable_daemon_falls_back_to_the_files(self, root, monkeypatch, daemon_for):
        install(root)
        daemon_for({"expanded": []}).close()
        diag = diagnose(root)
        assert diag.source == "files"
        assert diag.findings == []

    def test_a_root_the_daemon_does_not_serve_is_never_judged_on_its_rules(
        self, root, daemon_for, tmp_path
    ):
        install(root)
        daemon = daemon_for({"expanded": []}, folder_path=tmp_path / "elsewhere")
        rules, note = fetch_effective_rules(root)
        assert rules is None and "no Syncthing folder is rooted" in note
        assert daemon.requests == []

    def test_the_key_is_never_sent_off_loopback(self, root, monkeypatch):
        monkeypatch.setenv("SYNCTHING_API_URL", "http://192.0.2.1:8384")
        monkeypatch.setenv("SYNCTHING_API_KEY", "test-key")
        rules, note = fetch_effective_rules(root)
        assert rules is None and "not on loopback" in note


# ----------------------------------------------------------- include readers ---


class TestIncludeExpansion:
    def test_the_shim_expands_in_place_and_keeps_order(self, root):
        (root / ".stignore").write_text("first\n#include .stignore-shared\nlast\n")
        (root / ".stignore-shared").write_text("// c\nmiddle\n")
        text, missing = read_effective_text(root)
        assert [ln for ln in text.splitlines() if ln and not ln.startswith("//")] == [
            "first",
            "middle",
            "last",
        ]
        assert missing == []

    def test_a_missing_include_is_reported_not_silently_empty(self, root):
        (root / ".stignore").write_text(packaged_shim_text())
        text, missing = read_effective_text(root)
        assert missing == [".stignore-shared"]

    def test_an_include_outside_the_root_is_refused(self, root, tmp_path):
        (tmp_path / "outside").write_text("*.leak\n")
        (root / ".stignore").write_text("#include ../outside\n")
        text, missing = read_effective_text(root)
        assert "*.leak" not in text and missing == ["../outside"]

    def test_include_cycles_terminate(self, root):
        (root / ".stignore").write_text("#include .stignore-shared\n")
        (root / ".stignore-shared").write_text("a\n#include .stignore-shared\n")
        text, missing = read_effective_text(root)
        assert text.count("a\n") <= 10 and any("nesting" in m for m in missing)

    def test_an_unreadable_stignore_raises(self, root):
        with pytest.raises(OSError):
            read_effective_text(root)


class TestExistingAuditsSeeTheIncludedRules:
    """A shim node must not look like a node with no rules to the older checks."""

    def test_sync_audit_covers_every_private_material_class_through_the_include(self, root):
        from skcapstone.sync_policy.core import audit_folder
        from skcapstone.sync_policy.discovery import DiscoveredFolder

        install(root)
        report, _keys = audit_folder(DiscoveredFolder(folder_id="estate", path=root))
        categories = {f.category for f in report.findings}
        assert "private_pattern_uncovered" not in categories, report.findings
        assert "stignore_include_missing" not in categories

    def test_sync_audit_reports_a_dangling_include(self, root):
        from skcapstone.sync_policy.core import audit_folder
        from skcapstone.sync_policy.discovery import DiscoveredFolder

        (root / ".stignore").write_text(packaged_shim_text())
        report, _keys = audit_folder(DiscoveredFolder(folder_id="estate", path=root))
        assert "stignore_include_missing" in {f.category for f in report.findings}

    def test_fleet_stignore_doctor_is_clean_on_a_shim_node(self, root):
        from skcapstone.fleet import stignore_doctor

        install(root)
        report = stignore_doctor.check_folder(stignore_doctor.DEFAULT_RULESETS["skcapstone-sync"], root)
        assert report is not None and report.clean, report.findings()
