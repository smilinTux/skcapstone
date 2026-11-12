"""install.sh must install AND enable the skmemory-sync@<agent>.timer.

Card: skmemory sqlite-only-orphans, 2026-10-16. skmemory has shipped
skmemory-sync@.{service,timer} (plus a working standalone installer) since
v0.9.8, 2026-04-25. Nothing in this repo's own scripts/install.sh ever
called that installer or enabled the timer, so an agent home could exist
with zero sync coverage for six months with nothing catching it -- the
root cause of the detection gap behind that incident. skcapstone doctor's
memory:sync_timer check (card: same incident) is the gate that now catches
a skipped install; this test is the static guard that the install step
those checks expect to have run is actually wired into install.sh, since
the script's interactive prompts make full behavioral testing impractical
(see test_install_sh_no_phantom_units.py for the established convention).
"""

from pathlib import Path

# Repo root resolved from this file (tests/fleet/ -> repo root), so the test
# does not depend on pytest's working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALL_SH = (_REPO_ROOT / "scripts" / "install.sh").read_text()


def test_install_sh_copies_the_skmemory_sync_unit_templates():
    assert "skmemory-sync@.service" in _INSTALL_SH
    assert "skmemory-sync@.timer" in _INSTALL_SH


def test_install_sh_sources_the_units_from_the_skmemory_sibling_repo():
    assert '"$(dirname "$REPO_ROOT")/skmemory/systemd"' in _INSTALL_SH


def test_install_sh_enables_and_starts_the_timer_for_the_chosen_agent():
    assert 'systemctl --user enable --now "skmemory-sync@${_AGENT_NAME}.timer"' in _INSTALL_SH


def test_install_sh_does_not_sed_substitute_the_template_units():
    """skmemory-sync@.{service,timer} are systemd TEMPLATE units -- the
    instance name comes from the systemctl call, not from an =lumina
    substitution the way plain (non-templated) units get. Copying a
    template through the sed substitution used elsewhere in this loop
    would be harmless but pointless; assert the simpler `cp` path is used
    for these two files specifically."""
    idx = _INSTALL_SH.index("skmemory-sync@.service skmemory-sync@.timer")
    # The loop body for this block (a short, bounded window right after the
    # unit list) must use cp, not sed, for these units.
    window = _INSTALL_SH[idx : idx + 400]
    assert 'cp "$_src" "$_UNIT_DIR/$_unit"' in window
