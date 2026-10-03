"""The CI batch runner must preserve coverage of cases and fail closed."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

RUNNER = Path(__file__).parents[1] / "scripts/ci/run-pytest-batches.py"


def run_batches(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n integration: excluded\n e2e: excluded\n"
    )
    return subprocess.run(
        [sys.executable, str(RUNNER), "--batch-size", "1", "."],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        text=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.parametrize("first_fails", [False, True])
def test_batches_run_every_selected_case_and_preserve_failure(tmp_path, first_fails):
    for number in range(3):
        (tmp_path / f"test_{number}.py").write_text(
            "from pathlib import Path\nimport pytest\n"
            f'def test_selected():\n    Path("ran-{number}").write_text("yes")\n'
            f"    assert {not (number == 0 and first_fails)!r}\n"
            "@pytest.mark.integration\ndef test_excluded():\n    assert False\n"
        )
    result = run_batches(tmp_path)
    assert result.returncode == int(first_fails), result.stdout + result.stderr
    assert len(list(tmp_path.glob("ran-*"))) == 3
    assert "Selected 3 cases in 3 files" in result.stdout
    assert result.stdout.count("Batch completed with exit") == 3


def test_collection_error_cannot_become_an_empty_success(tmp_path):
    (tmp_path / "test_broken.py").write_text("this is not valid python !!!")
    result = run_batches(tmp_path)
    assert result.returncode != 0
    assert "SyntaxError" in result.stdout
    assert "Batch completed" not in result.stdout


def test_recorded_output_does_not_wait_for_descendant_log_handles():
    import runpy
    import signal

    run_recorded = runpy.run_path(str(RUNNER))["run_recorded"]
    result = run_recorded(
        [
            sys.executable,
            "-c",
            "import subprocess, sys; "
            'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"]); '
            "print(child.pid, flush=True)",
        ],
        timeout=5,
    )
    child = int(result.stdout.strip())
    try:
        assert result.returncode == 0
    finally:
        os.kill(child, signal.SIGTERM)


def test_recorded_timeout_preserves_output_and_failure():
    import runpy

    run_recorded = runpy.run_path(str(RUNNER))["run_recorded"]
    result = run_recorded(
        [sys.executable, "-c", 'import time; print("before timeout", flush=True); time.sleep(30)'],
        timeout=0.5,
    )
    assert result.returncode == 124
    assert "before timeout" in result.stdout
    assert "Command exceeded" in result.stdout
