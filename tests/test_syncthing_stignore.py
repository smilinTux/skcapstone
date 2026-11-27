"""_write_stignore must never destroy an existing ignore rule (card 20a1d4d3).

The rules in a live .stignore are load-bearing incident history. Losing
`**/comms/outbox` brings back the seed outbox flood; losing the SQLite
`-shm`/`-wal` rules replicates a live database into corruption; losing
`(?d)**/*.tmp` reinstates a scanner-abort bug whose fix is dated in the file
itself. `_merge_stignore` (still used by the sync policy remediation) is
union-only: the template may ADD, and may never remove. `_write_stignore` goes
further: it installs the shim plus `.stignore-shared` layout and never rewrites a
legacy file unless `migrate` is passed, and then keeps every rule.
"""

from __future__ import annotations

import pytest

from skcapstone.skills import syncthing_setup as ss
from skcapstone.stignore_layout import packaged_shared_text, packaged_shim_text

TEMPLATE = """\
// bundled template
*.key
*.pem
**/private.*
**/memory/index.db
"""

# A live file that has drifted ahead of the template, as the real one had.
LIVE = """\
// SKCapstone Sovereign Singularity - Syncthing ignore rules
// Private key material must never leave this node
*.key
*.pem
**/private.*

// transient atomic-write temp files - were aborting the scanner (fix 2026-06-10)
(?d)**/*.tmp

// seed outbox flood: housekeeping never pruned these
**/comms/outbox

// SQLite runtime files - always local, never sync
**/memory/index.db-shm
**/memory/index.db-wal
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the module's AGENT_HOME at a throwaway tree."""
    root = tmp_path / ".skcapstone"
    root.mkdir()
    monkeypatch.setattr(ss, "AGENT_HOME", root)
    return root


# ------------------------------------------------------------ pure merge ---


def test_merge_preserves_every_existing_pattern() -> None:
    merged = ss._merge_stignore(LIVE, TEMPLATE)
    for pattern in ss._pattern_lines(LIVE):
        assert pattern in ss._pattern_lines(merged), f"lost rule: {pattern}"


def test_merge_preserves_the_comments_that_explain_why() -> None:
    """The comments are the only record of which incident bought each rule."""
    merged = ss._merge_stignore(LIVE, TEMPLATE)
    assert "fix 2026-06-10" in merged
    assert "seed outbox flood" in merged


def test_merge_adds_template_patterns_the_live_file_lacks() -> None:
    merged = ss._merge_stignore(LIVE, TEMPLATE)
    assert "**/memory/index.db" in ss._pattern_lines(merged)


def test_merge_is_a_noop_when_the_template_adds_nothing() -> None:
    covered = LIVE + "\n**/memory/index.db\n"
    assert ss._merge_stignore(covered, TEMPLATE) == covered


def test_merge_never_shrinks_the_pattern_set() -> None:
    merged = ss._merge_stignore(LIVE, TEMPLATE)
    assert len(ss._pattern_lines(merged)) >= len(ss._pattern_lines(LIVE))


def test_pattern_lines_ignores_comments_and_blanks() -> None:
    assert ss._pattern_lines("// c\n\n  *.key  \n// x\n*.pem\n") == ["*.key", "*.pem"]


# ----------------------------------------------------------- file effects ---


def test_missing_file_gets_shared_file_then_shim(home) -> None:
    path = ss._write_stignore()
    assert path == home / ".stignore"
    assert path.read_text() == packaged_shim_text()
    assert (home / ".stignore-shared").read_text() == packaged_shared_text()


def test_existing_legacy_file_is_never_rewritten(home) -> None:
    """The regression this card exists for: one `sync setup` used to wipe
    every rule that had accrued past the bundled template."""
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")

    ss._write_stignore()

    assert path.read_text() == LIVE


def test_a_backup_is_written_once_for_a_legacy_file(home) -> None:
    """Re-running must be free: ensure_skeleton runs on every daemon start."""
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")

    ss._write_stignore()
    ss._write_stignore()

    backups = list(home.glob(".stignore.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == LIVE


def test_migrate_keeps_every_legacy_rule(home) -> None:
    path = home / ".stignore"
    path.write_text(LIVE + "\n// this host only\n**/scratch-only\n", encoding="utf-8")

    ss._write_stignore(migrate=True)

    after = path.read_text()
    assert "#include .stignore-shared" in after
    # Every legacy pattern is either in the shared file or kept node-local.
    shared = set(ss._pattern_lines((home / ".stignore-shared").read_text()))
    for pattern in ss._pattern_lines(LIVE):
        assert pattern in shared or pattern in ss._pattern_lines(after), pattern
    assert "**/scratch-only" in after  # not in the shared file, so kept local
    assert "// this host only" in after


def test_unreadable_existing_file_is_left_alone(home, monkeypatch) -> None:
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")
    real = type(path).read_text

    def boom(self, *a, **k):
        if self.name == ".stignore":
            raise OSError("permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(path), "read_text", boom)
    ss._write_stignore()
    monkeypatch.undo()
    assert path.read_text() == LIVE
    assert not (home / ".stignore-shared").exists()
