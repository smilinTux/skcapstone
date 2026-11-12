"""Review startup resolves the installed wrapper without relying on daemon PATH."""

import pytest

from skcapstone.fleet import production_builder


def test_review_wrapper_is_found_beside_interpreter_without_venv_on_path(tmp_path, monkeypatch):
    binary = tmp_path / "venv" / "bin"
    binary.mkdir(parents=True)
    wrapper = binary / "skfleet-worker-wrapper.py"
    wrapper.write_text("# installed wrapper\n")
    monkeypatch.setattr("sys.executable", str(binary / "python"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    assert production_builder.review_wrapper_path() == str(wrapper)


def test_missing_review_wrapper_names_every_path_tried(tmp_path, monkeypatch):
    binary = tmp_path / "venv" / "bin"
    binary.mkdir(parents=True)
    monkeypatch.setattr("sys.executable", str(binary / "python"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    with pytest.raises(ValueError) as exc:
        production_builder.review_wrapper_path()

    message = str(exc.value)
    assert str(binary / "skfleet-worker-wrapper.py") in message
    assert "/usr/bin/skfleet-worker-wrapper.py" in message
    assert "/bin/skfleet-worker-wrapper.py" in message
