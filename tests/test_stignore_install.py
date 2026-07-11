"""Tests for C3 — bundled .stignore template + idempotent install."""

from __future__ import annotations

from pathlib import Path

import skcapstone


def _template_text() -> str:
    tmpl = Path(skcapstone.__file__).parent / "defaults" / ".stignore"
    return tmpl.read_text(encoding="utf-8")


class TestTemplateContents:
    def test_template_file_exists(self):
        tmpl = Path(skcapstone.__file__).parent / "defaults" / ".stignore"
        assert tmpl.is_file()

    def test_excludes_derived_and_runtime(self):
        text = _template_text()
        for pattern in (
            "**/memory/chroma",
            "**/chroma.bak*",
            "**/memory/index.db",
            "**/*.db-wal",
            "**/*.db-shm",
            "**/*.sync-conflict-*",
            "**/*.pid",
            "daemon.pid",
            "**/daemon.log",
            "logs",
            "sessions",
            "conversations",
            "backups",
            "deployments",
            "pubsub",
            "file-transfer",
            "**/telegram.session",
            "**/skwhisper/state.json",
            "**/memory/archive",
            "**/comms/archive",
            "__pycache__",
        ):
            assert pattern in text, f"missing derived-exclude rule: {pattern!r}"

    def test_keeps_source_of_truth(self):
        """Source-of-truth trees must NOT be excluded wholesale."""
        text = _template_text()
        lines = {ln.strip() for ln in text.splitlines()}
        # No blanket exclude of the memory tiers, soul, seeds, journal, febs, coord.
        for forbidden in (
            "memory/short-term",
            "memory/mid-term",
            "memory/long-term",
            "soul",
            "seeds",
            "journal.md",
            "trust/febs",
            "coordination",
            "comms/inbox",
            "comms/outbox",
        ):
            assert forbidden not in lines, f"must not exclude source-of-truth: {forbidden!r}"


class TestInstallDefaultStignore:
    def test_installs_when_absent(self, tmp_path):
        skcapstone._install_default_stignore(tmp_path)
        dest = tmp_path / ".stignore"
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == _template_text()

    def test_never_overwrites_existing(self, tmp_path):
        dest = tmp_path / ".stignore"
        dest.write_text("// operator custom rules\n*.secret\n", encoding="utf-8")

        skcapstone._install_default_stignore(tmp_path)

        assert dest.read_text(encoding="utf-8") == "// operator custom rules\n*.secret\n"

    def test_idempotent(self, tmp_path):
        skcapstone._install_default_stignore(tmp_path)
        first = (tmp_path / ".stignore").read_text(encoding="utf-8")
        skcapstone._install_default_stignore(tmp_path)
        second = (tmp_path / ".stignore").read_text(encoding="utf-8")
        assert first == second


class TestSyncthingSetupUsesTemplate:
    def test_stignore_contents_matches_template(self):
        from skcapstone.skills import syncthing_setup

        assert syncthing_setup.STIGNORE_CONTENTS == _template_text()
