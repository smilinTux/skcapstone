from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "worktree-hygiene.py"


def load_module():
    spec = importlib.util.spec_from_file_location("worktree_hygiene", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "file.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c1"], check=True)
    return repo


def plant_stale_sequencer(repo: Path):
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    gd = Path(out) if out.startswith("/") else repo / out
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    seq = gd / "sequencer"
    seq.mkdir(exist_ok=True)
    (seq / "todo").write_text("pick 0836afe example\n")
    (seq / "head").write_text(sha + "\n")
    (seq / "abort-safety").write_text(sha + "\n")


def repo_git_dir(repo: Path) -> Path:
    out = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out) if out.startswith("/") else repo / out


def run_script(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)


def test_clean_repo_reports_ok(tmp_path):
    repo = make_repo(tmp_path)
    r = run_script(str(repo))
    assert r.returncode == 0 and "OK" in r.stdout


def test_stale_clean_tree_report_then_clear(tmp_path):
    repo = make_repo(tmp_path)
    plant_stale_sequencer(repo)
    r = run_script(str(repo))
    assert r.returncode == 1 and "sequencer" in r.stdout and "clean tree" in r.stdout
    r = run_script("--clear", str(repo))
    assert r.returncode == 0 and "CLEARED" in r.stdout
    assert not (repo_git_dir(repo) / "sequencer").exists()


def test_stale_dirty_tree_blocks_even_with_clear(tmp_path):
    repo = make_repo(tmp_path)
    plant_stale_sequencer(repo)
    (repo / "file.txt").write_text("dirty\n")
    r = run_script("--clear", str(repo))
    assert r.returncode == 2 and "DIRTY" in r.stdout and "never auto-cleared" in r.stdout
    assert (repo_git_dir(repo) / "sequencer").exists()


def test_linked_worktree_markers_are_found(tmp_path):
    repo = make_repo(tmp_path, "main-repo")
    wt = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(wt), "-b", "side"], check=True
    )
    plant_stale_sequencer(wt)
    r = run_script(str(wt))
    assert r.returncode == 1 and "sequencer" in r.stdout


def test_not_a_repo_errors(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_script(str(empty))
    assert r.returncode == 3 and "not a git worktree" in r.stdout


def test_detect_unit(tmp_path):
    m = load_module()
    repo = make_repo(tmp_path, "unit")
    assert m.detect(str(repo))["state"] == "OK"
    plant_stale_sequencer(repo)
    f = m.detect(str(repo))
    assert f["state"] == "CLEAN_TREE" and f["markers"] == ["sequencer"]
