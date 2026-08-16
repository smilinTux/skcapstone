from pathlib import Path


def test_install_sh_references_no_phantom_skchat_units():
    text = Path("scripts/install.sh").read_text()
    for phantom in ("skchat-lumina-bridge", "skchat-bridges.target"):
        assert phantom not in text, f"scripts/install.sh still references {phantom}"
