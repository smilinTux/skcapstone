from pathlib import Path

# Repo root resolved from this file (tests/fleet/ -> repo root), so the test does
# not depend on pytest's working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_install_sh_references_no_phantom_skchat_units():
    text = (_REPO_ROOT / "scripts" / "install.sh").read_text()
    for phantom in ("skchat-lumina-bridge", "skchat-bridges.target"):
        assert phantom not in text, f"scripts/install.sh still references {phantom}"
