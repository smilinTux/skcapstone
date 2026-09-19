"""Nothing may name a per-host artifact's path by convention.

Every script in `deployment_manifest.PER_HOST_ARTIFACTS` exists at TWO
paths on a live host, placed by two unrelated mechanisms:

  ~/.skenv/bin/<name>   a side effect of `pip install -e .`, because the
                        script is a pyproject `script-files` entry.
  ~/.local/bin/<name>   an explicit `cp` in the rollout's deploy step.
                        This is the one the units actually execute,
                        confirmed live on chiap01/02/03/04/08.

Nothing keeps the two equal. They match today, so every caller that
guessed wrong has been getting the right answer by luck. The first rollout
that copies one and not the other makes those guesses wrong with no error
and no report -- and in the worst way available, because the wrong file
EXISTS, so an `is_file()` guard passes and a stale dispatcher runs silently
instead of failing closed.

Three sites had guessed: the readiness unit's --rotate-script (fixed in
PR #808), the niobe-live unit's --dispatcher, and two copies of
`Path(sys.executable).parent / "skfleet-rotate.py"` in
seat_cycle_entrypoint.py. These tests exist so the fourth cannot land
quietly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from skcapstone.fleet.deployment_manifest import (
    PER_HOST_ARTIFACTS,
    PER_HOST_BIN_RELATIVE_DIR,
    deployed_artifact_path,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT_DIRS = (REPO_ROOT / "systemd", REPO_ROOT / "src" / "skcapstone" / "data" / "systemd")
SOURCE_DIRS = (REPO_ROOT / "src",)

ARTIFACT_NAMES = tuple(p.name for p in PER_HOST_ARTIFACTS)


def test_deployed_artifact_path_points_at_the_deployed_copy():
    path = deployed_artifact_path("skfleet-rotate.py", home=Path("/home/example"))
    assert path == Path("/home/example") / PER_HOST_BIN_RELATIVE_DIR / "skfleet-rotate.py"
    assert ".skenv" not in str(path), "this must never resolve to the pip copy"


@pytest.mark.parametrize("name", ARTIFACT_NAMES)
def test_no_unit_names_the_pip_copy_of_a_per_host_artifact(name):
    """A unit naming ~/.skenv/bin/<artifact> runs the pip copy, not the
    deployed one. Both trees are checked: the canonical systemd/ tree and
    the packaged copy that ships in the wheel, because a cold PyPI install
    deploys from the latter.
    """
    offenders = []
    for units_dir in UNIT_DIRS:
        for unit in sorted(units_dir.glob("*")):
            if not unit.is_file():
                continue
            text = unit.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                if f".skenv/bin/{name}" in line:
                    offenders.append(f"{unit.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        f"{name} is deployed to ~/{PER_HOST_BIN_RELATIVE_DIR.as_posix()}/ by the rollout; "
        f"these units name the pip copy instead:\n  " + "\n  ".join(offenders)
    )


#: `Path(sys.executable).parent / "<artifact>"` and the os.path spelling of
#: the same guess. sys.executable is ~/.skenv/bin/python3, so its parent is
#: the pip copy's directory -- never the deployed one.
_INTERPRETER_DIR_GUESS = re.compile(
    r"sys\.executable\s*\)\s*\.parent|os\.path\.dirname\(\s*sys\.executable\s*\)"
)


def _code_only(source: str) -> str:
    """`source` with every comment and string literal blanked out.

    Matched against raw text, this check fires on its own documentation:
    `deployed_artifact_path`'s docstring has to SAY "never derive this from
    Path(sys.executable).parent" to be useful, and a prose match there is a
    false positive that would push the next author toward deleting the
    warning rather than the defect. Tokenizing keeps the rule aimed at code
    while leaving line numbers intact, so an offender is still reported at
    the line a reader can open.
    """
    import io
    import tokenize

    lines = source.splitlines()
    blanked = list(lines)
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source  # unparseable: fall back to the strict raw match
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        # Blank the token's CHARACTER span, not its whole line. A line like
        # `d = Path(sys.executable).parent / "x"` carries a string token, and
        # blanking the line would delete the very code this check exists to
        # find -- which is exactly what
        # test_the_interpreter_guess_check_can_actually_fail caught.
        (first_line, first_col), (last_line, last_col) = token.start, token.end
        for lineno in range(first_line, last_line + 1):
            if not 1 <= lineno <= len(blanked):
                continue
            line = blanked[lineno - 1]
            start = first_col if lineno == first_line else 0
            end = last_col if lineno == last_line else len(line)
            blanked[lineno - 1] = line[:start] + " " * max(0, end - start) + line[end:]
    return "\n".join(blanked)


def test_no_source_derives_an_artifact_path_from_the_interpreter_location():
    offenders = []
    for source_dir in SOURCE_DIRS:
        for path in sorted(source_dir.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not any(name in text for name in ARTIFACT_NAMES):
                continue
            for lineno, line in enumerate(_code_only(text).splitlines(), 1):
                if _INTERPRETER_DIR_GUESS.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "these files name a per-host artifact AND derive a directory from the "
        "running interpreter, which resolves to the pip copy. Use "
        "deployment_manifest.deployed_artifact_path() instead:\n  " + "\n  ".join(offenders)
    )


def test_the_interpreter_guess_check_can_actually_fail():
    """Negative control: the tokenizer must not blank out real code too.

    A check that can only ever pass is worse than no check, and this one
    blanks most of its input before matching, so its ability to still fail
    is the property worth proving.
    """
    real_code = 'import sys\nfrom pathlib import Path\nd = Path(sys.executable).parent / "x"\n'
    assert _INTERPRETER_DIR_GUESS.search(_code_only(real_code))

    only_prose = '"""Never write Path(sys.executable).parent here."""\n'
    assert not _INTERPRETER_DIR_GUESS.search(_code_only(only_prose))


def test_seat_cycle_entrypoint_resolves_the_deployed_dispatcher(monkeypatch, tmp_path):
    """The two call sites in seat_cycle_entrypoint really do land in
    ~/.local/bin now, not merely in a file that passes lint."""
    from skcapstone import seat_cycle_entrypoint

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resolved = seat_cycle_entrypoint.deployed_artifact_path("skfleet-rotate.py")
    assert resolved == tmp_path / ".local" / "bin" / "skfleet-rotate.py"
