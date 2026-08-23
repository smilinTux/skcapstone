"""_write_stignore must never destroy an existing ignore rule (card 20a1d4d3).

The rules in a live .stignore are load-bearing incident history. Losing
`**/comms/outbox` brings back the seed outbox flood; losing the SQLite
`-shm`/`-wal` rules replicates a live database into corruption; losing
`(?d)**/*.tmp` reinstates a scanner-abort bug whose fix is dated in the file
itself. So the only safe merge direction is union: the template may ADD, and
may never remove.
"""

from __future__ import annotations

import pytest

from skcapstone.skills import syncthing_setup as ss

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
    monkeypatch.setattr(ss, "STIGNORE_CONTENTS", TEMPLATE)
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


def test_missing_file_gets_the_template(home) -> None:
    path = ss._write_stignore()
    assert path.read_text() == TEMPLATE


def test_existing_file_is_never_reverted(home) -> None:
    """The regression this card exists for: one `sync setup` used to wipe
    every rule that had accrued past the bundled template."""
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")

    ss._write_stignore()

    after = path.read_text()
    assert "**/comms/outbox" in after
    assert "(?d)**/*.tmp" in after
    assert "**/memory/index.db-shm" in after
    assert after != TEMPLATE


def test_a_backup_is_written_before_any_change(home) -> None:
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")

    ss._write_stignore()

    backups = list(home.glob(".stignore.bak-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == LIVE


def test_no_backup_and_no_write_when_nothing_changes(home) -> None:
    """Re-running must be free, or it churns a file inside a synced folder."""
    path = home / ".stignore"
    path.write_text(LIVE + "\n**/memory/index.db\n", encoding="utf-8")
    before = path.read_text()

    ss._write_stignore()
    ss._write_stignore()

    assert path.read_text() == before
    assert list(home.glob(".stignore.bak-*")) == []


def test_private_key_rules_survive_a_template_that_dropped_them(home) -> None:
    """Even a template regression cannot strip protection from a live node,
    because the template can only add."""
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")
    ss.STIGNORE_CONTENTS = "// oops, no private key rules\nvenv\n"

    ss._write_stignore()

    after = ss._pattern_lines(path.read_text())
    assert "*.key" in after
    assert "*.pem" in after
    assert "**/private.*" in after


def test_unreadable_existing_file_is_left_alone(home, monkeypatch) -> None:
    path = home / ".stignore"
    path.write_text(LIVE, encoding="utf-8")

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(type(path), "read_text", boom)
    ss._write_stignore()
    monkeypatch.undo()
    assert path.read_text() == LIVE
