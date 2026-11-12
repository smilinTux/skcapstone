"""Tests for the packaged Syncthing ignore layout and its install.

The layout (sk-standards SYNCTHING_IGNORE_STANDARD): a synced ``.stignore-shared``
holds the rules, every node's ``.stignore`` is a 3-line shim that includes it.
Every test installs into a ``tmp_path``; none touches the real ``~/.skcapstone``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import skcapstone
from skcapstone.stignore_layout import (
    CRITICAL_RULES,
    INCLUDE_LINE,
    classify,
    install,
    packaged_shared_text,
    packaged_shim_text,
    pattern_lines,
)
from skcapstone.sync_policy.model import MATERIAL_CLASSES
from skcapstone.sync_policy.stignore import Coverage, evaluate, load_ruleset

DEFAULTS = Path(skcapstone.__file__).parent / "defaults"


class TestPackagedData:
    def test_both_files_ship(self):
        assert (DEFAULTS / ".stignore").is_file()
        assert (DEFAULTS / ".stignore-shared").is_file()

    def test_skeleton_is_a_three_line_shim(self):
        text = packaged_shim_text()
        assert classify(text) == "shim"
        assert INCLUDE_LINE in text.splitlines()
        assert len(text.strip().splitlines()) == 3
        assert pattern_lines(text) == [], "the shim must hold no rules of its own"

    def test_shared_covers_every_critical_rule(self):
        patterns = set(pattern_lines(packaged_shared_text()))
        for rule in CRITICAL_RULES:
            assert rule in patterns, f"canonical shared file lacks {rule!r}"

    def test_shared_covers_every_private_material_probe(self):
        """The sync policy audit probes these classes; the shared file must cover them."""
        rules = load_ruleset(packaged_shared_text())
        for material in MATERIAL_CLASSES:
            for probe, _line in material.probes:
                assert evaluate(rules, probe) is Coverage.IGNORED, probe

    def test_shared_ignores_the_skwhisper_derived_outputs(self):
        rules = load_ruleset(packaged_shared_text())
        for name in ("state.json", "whisper.md", "health.json", "patterns.json"):
            assert evaluate(rules, f"agents/lumina/skwhisper/{name}") is Coverage.IGNORED

    def test_shared_keeps_source_of_truth(self):
        """Source-of-truth trees must NOT be excluded wholesale."""
        rules = load_ruleset(packaged_shared_text())
        for path in (
            "agents/lumina/memory/long-term/abc.json",
            "agents/lumina/soul/base.json",
            "agents/lumina/seeds/s.json",
            "agents/lumina/trust/febs/a.feb",
            "coordination/tasks/t.json",
            "agents/lumina/journal.md",
        ):
            assert evaluate(rules, path) is Coverage.EXPOSED, path

    def test_shared_has_no_negation_and_no_include(self):
        """No `!` (order-dependent) and no nested include (a shared file is a leaf)."""
        for line in pattern_lines(packaged_shared_text()):
            assert not line.lstrip("(?di)").startswith("!"), line
        assert "#include" not in packaged_shared_text()

    def test_no_em_or_en_dashes(self):
        for name in (".stignore", ".stignore-shared"):
            text = (DEFAULTS / name).read_text(encoding="utf-8")
            assert "—" not in text and "–" not in text, name


class TestInstallFresh:
    def test_writes_shared_then_shim(self, tmp_path):
        result = install(tmp_path)
        assert result.shared == "written"
        assert result.stignore == "written"
        assert (tmp_path / ".stignore-shared").read_text() == packaged_shared_text()
        assert (tmp_path / ".stignore").read_text() == packaged_shim_text()

    def test_shared_exists_before_the_shim_is_visible(self, tmp_path, monkeypatch):
        """A shim over a missing include errors the Syncthing folder: order is the fix."""
        from skcapstone import stignore_layout

        seen = []
        real = stignore_layout._atomic_write

        def spy(path, text):
            seen.append((path.name, (tmp_path / ".stignore-shared").exists()))
            real(path, text)

        monkeypatch.setattr(stignore_layout, "_atomic_write", spy)
        install(tmp_path)
        assert seen == [(".stignore-shared", False), (".stignore", True)]

    def test_idempotent(self, tmp_path):
        install(tmp_path)
        first = {p.name: p.read_text() for p in tmp_path.iterdir()}
        again = install(tmp_path)
        assert not again.changed
        assert {p.name: p.read_text() for p in tmp_path.iterdir()} == first

    def test_creates_a_missing_root(self, tmp_path):
        root = tmp_path / "nested" / ".skcapstone"
        assert install(root).changed
        assert (root / ".stignore").is_file()

    def test_ensure_skeleton_entry_point_installs_the_layout(self, tmp_path):
        skcapstone._install_default_stignore(tmp_path)
        assert (tmp_path / ".stignore-shared").is_file()
        assert classify((tmp_path / ".stignore").read_text()) == "shim"

    def test_ensure_skeleton_never_overwrites_an_existing_file(self, tmp_path):
        dest = tmp_path / ".stignore"
        dest.write_text("// operator custom rules\n*.secret\n", encoding="utf-8")
        skcapstone._install_default_stignore(tmp_path)
        assert dest.read_text(encoding="utf-8") == "// operator custom rules\n*.secret\n"


class TestInstallExistingShim:
    def test_shim_is_untouched(self, tmp_path):
        shim = "// mine\n#include .stignore-shared\n*.local-only\n"
        (tmp_path / ".stignore").write_text(shim)
        (tmp_path / ".stignore-shared").write_text("*.key\n")
        result = install(tmp_path)
        assert result.stignore == "shim" and result.shared == "present"
        assert not result.changed
        assert (tmp_path / ".stignore").read_text() == shim
        assert (tmp_path / ".stignore-shared").read_text() == "*.key\n", "never replaced"

    def test_missing_shared_is_created_for_an_existing_shim(self, tmp_path):
        (tmp_path / ".stignore").write_text(packaged_shim_text())
        result = install(tmp_path)
        assert result.shared == "written" and result.stignore == "shim"
        assert (tmp_path / ".stignore-shared").read_text() == packaged_shared_text()


LEGACY = """\
// my old full ruleset
*.key
*.pem
**/private.*
// node-local: this host mounts a scratch disk
scratch-disk
(?d)*.scratch
(?d)**/*.tmp
"""

WORKER = """\
*.key
*.pem
**/private.*
*.env
!/fleet
!/fleet/**
**
"""


class TestInstallExistingNonShim:
    def test_legacy_is_not_overwritten_but_backed_up_and_reported(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        result = install(tmp_path)
        assert result.stignore == "legacy"
        assert (tmp_path / ".stignore").read_text() == LEGACY
        assert result.backup is not None and result.backup.read_text() == LEGACY
        assert "scratch-disk" in result.local_only
        assert "/secrets" in result.gained, "the difference names what the node would gain"
        assert any("--migrate" in m for m in result.messages)

    def test_repeated_runs_make_one_backup(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        install(tmp_path)
        install(tmp_path)
        assert len(list(tmp_path.glob(".stignore.bak-*"))) == 1

    def test_migrate_converts_to_a_shim_and_keeps_node_local_rules(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        result = install(tmp_path, migrate=True)
        assert result.stignore == "migrated"
        text = (tmp_path / ".stignore").read_text()
        assert classify(text) == "shim"
        assert text.splitlines()[2] == INCLUDE_LINE
        assert "scratch-disk" in text
        assert "// node-local: this host mounts a scratch disk" in text, "comment travels"
        assert "(?d)*.scratch" in text, "flag prefixes survive"
        assert "(?d)**/*.tmp" not in text, "already in the shared file, so not kept twice"
        # duplicates of shared rules are dropped, not kept twice
        assert "*.pem" not in pattern_lines(text)
        assert result.backup is not None and result.backup.read_text() == LEGACY

    def test_migrate_preserves_every_rule(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        install(tmp_path, migrate=True)
        kept = set(pattern_lines((tmp_path / ".stignore").read_text()))
        shared = set(pattern_lines((tmp_path / ".stignore-shared").read_text()))
        for pattern in pattern_lines(LEGACY):
            assert pattern in kept | shared, pattern

    def test_migrate_refuses_a_file_with_a_negation(self, tmp_path):
        text = "*.key\n!keep.key\n"
        (tmp_path / ".stignore").write_text(text)
        result = install(tmp_path, migrate=True)
        assert result.stignore == "refused"
        assert (tmp_path / ".stignore").read_text() == text

    def test_worker_allowlist_is_left_alone_and_gets_no_shared_file(self, tmp_path):
        (tmp_path / ".stignore").write_text(WORKER)
        result = install(tmp_path, migrate=True)
        assert result.stignore == "worker" and result.shared == "skipped"
        assert (tmp_path / ".stignore").read_text() == WORKER
        assert not (tmp_path / ".stignore-shared").exists()
        assert list(tmp_path.glob(".stignore.bak-*")) == []


class TestClassify:
    @pytest.mark.parametrize(
        ("text", "kind"),
        [
            (None, "absent"),
            ("#include .stignore-shared\n", "shim"),
            ("// c\n  #include .stignore-shared  \n*.x\n", "shim"),
            (WORKER, "worker"),
            ("!/a\n*.key\n", "legacy"),  # a negation without a catch-all is not a worker
            (LEGACY, "legacy"),
            ("", "legacy"),
        ],
    )
    def test_kinds(self, text, kind):
        assert classify(text) == kind


class TestInitInstallsTheLayout:
    """`skcapstone init` (the onboard wizard) must install the layout, not leave it to a
    later preflight: Syncthing can see the folder as soon as init creates it."""

    def test_onboard_identity_step_installs_shared_then_shim(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        from skcapstone import onboard

        monkeypatch.setenv("HOME", str(tmp_path))
        fake = SimpleNamespace(fingerprint="ABCD" * 10, status=SimpleNamespace(value="active"))
        monkeypatch.setattr("skcapstone.pillars.identity.generate_identity", lambda *a, **k: fake)
        home = tmp_path / ".skcapstone"

        onboard._step_identity(home, "Tester", None)

        assert (home / ".stignore-shared").read_text() == packaged_shared_text()
        assert classify((home / ".stignore").read_text()) == "shim"


class TestSyncStignoreCommand:
    def _run(self, home, *args):
        from click.testing import CliRunner

        from skcapstone.cli import main

        return CliRunner().invoke(main, ["sync", "stignore", "--home", str(home), *args])

    def test_fresh_home_gets_the_layout(self, tmp_path):
        result = self._run(tmp_path)
        assert result.exit_code == 0, result.output
        assert classify((tmp_path / ".stignore").read_text()) == "shim"
        assert (tmp_path / ".stignore-shared").is_file()

    def test_legacy_is_reported_not_rewritten_without_migrate(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        result = self._run(tmp_path)
        assert result.exit_code == 0, result.output
        assert "scratch-disk" in result.output, "names the rules only the node has"
        assert (tmp_path / ".stignore").read_text() == LEGACY

    def test_migrate_converts_it(self, tmp_path):
        (tmp_path / ".stignore").write_text(LEGACY)
        result = self._run(tmp_path, "--migrate")
        assert result.exit_code == 0, result.output
        assert classify((tmp_path / ".stignore").read_text()) == "shim"

    def test_a_refused_migration_exits_nonzero(self, tmp_path):
        (tmp_path / ".stignore").write_text("*.key\n!keep.key\n")
        assert self._run(tmp_path, "--migrate").exit_code == 1
