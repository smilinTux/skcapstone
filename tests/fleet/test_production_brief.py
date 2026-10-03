"""Execute the rendered handoff against real Git and a mediated CLI stand-in."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet.production_brief import production_completion_recipe, production_worker_brief

CARD = "c142ef10"
OWNER = "pi-deepseek-builder-node-c142ef10"
CLAIM = "a" * 32


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "isolated clone"
    root.mkdir()
    git(root, "init", "-b", "work/source")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "user.name", "Fixture")
    (root / "source.py").write_text("value = 1\n")
    git(root, "add", "source.py")
    git(root, "commit", "-m", "base")
    base = git(root, "rev-parse", "HEAD")
    evidence = root / f"docs/evidence/agents/{CARD}/COMPLETION-EVIDENCE.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b"Actual fixture test: PASS\nLimitations: synthetic only.\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "candidate evidence")
    home = tmp_path / "private home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "coord-writes.jsonl"
    cli = binaries / "skcapstone"
    cli.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['coord','show']:
    print(json.dumps({'id':'c142ef10','owner':os.environ['TEST_OWNER'],
                     'status':'doing','meta':{'_claim_revision':os.environ['TEST_CLAIM']}}))
else:
    assert args[0]=='coord' and args[1] in ('link','verdict')
    assert args[args.index('--agent')+1]==os.environ['TEST_OWNER']
    assert '--expected-claim-revision' not in args and '--expected-source-revision' not in args
    with Path(os.environ['TEST_LOG']).open('a') as out:out.write(json.dumps(args)+'\\n')
""")
    cli.chmod(0o700)
    environment = {
        **os.environ,
        "HOME": str(home),
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "TEST_OWNER": OWNER,
        "TEST_CLAIM": CLAIM,
        "TEST_LOG": str(log),
    }
    return root, evidence, base, home, log, environment


def prompt(source):
    root, _, base, _, _, _ = source
    return production_worker_brief(
        card_id=CARD,
        owner=OWNER,
        claim_revision=CLAIM,
        workspace=str(root),
        base_revision=base,
        title="[S] Fixture",
        description="Local commit authorized; no push.",
        acceptance_criteria=["Actual required fixture checks pass"],
    )


def execute(source, umask=None):
    recipe = prompt(source).split("```bash\n", 1)[1].split("```", 1)[0]
    return subprocess.run(
        ["bash", "-c", recipe],
        cwd=source[0],
        env=source[-1],
        capture_output=True,
        text=True,
        **({"umask": umask} if umask is not None else {}),
    )


def test_rendered_recipe_binds_real_committed_bytes_and_current_revision(source):
    root, evidence, _, home, log, _ = source
    result = execute(source)
    assert result.returncode == 0, result.stderr
    writes = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(writes) == 3 and writes[-1][:4] == ["coord", "verdict", CARD, "PASS_FOR_REVIEW"]
    assert [row[3] for row in writes[:-1]] == ["commit_sha", "branch"]
    verdict = writes[-1]
    candidate = Path(verdict[verdict.index("--candidate") + 1])
    assert candidate.parent == home / f".skcapstone/evidence/work/{CARD}"
    assert candidate.read_bytes() == evidence.read_bytes()
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert candidate.parent.stat().st_mode & 0o777 == 0o700
    assert verdict[verdict.index("--commit") + 1] == git(root, "rev-parse", "HEAD")
    assert verdict[verdict.index("--tree") + 1] == git(root, "rev-parse", "HEAD^{tree}")
    assert verdict[verdict.index("--ref") + 1] == "refs/heads/work/source"
    assert git(root, "status", "--porcelain") == ""
    assert git(root, "rev-parse", "HEAD") in result.stdout


@pytest.mark.parametrize("mask", [0, 0o002, 0o022])
def test_staging_is_private_despite_permissive_umask(source, mask):
    result = execute(source, umask=mask)
    assert result.returncode == 0, result.stderr
    directory = source[3] / f".skcapstone/evidence/work/{CARD}"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert next(directory.iterdir()).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", [0o775, 0o755, 0o770])
def test_existing_unsafe_directory_is_refused_without_chmod_or_board_write(source, mode):
    directory = source[3] / f".skcapstone/evidence/work/{CARD}"
    directory.mkdir(parents=True)
    directory.chmod(mode)
    result = execute(source)
    assert result.returncode != 0
    assert "must be owned and mode 0700" in result.stderr
    assert directory.stat().st_mode & 0o777 == mode
    assert not list(directory.iterdir()) and not source[4].exists()


@pytest.mark.parametrize("component", [".skcapstone", "evidence", "work", CARD])
def test_staging_refuses_symlink_directory_components(source, tmp_path, component):
    destination = tmp_path / "unrelated"
    destination.mkdir(mode=0o700)
    directory = source[3]
    for part in (".skcapstone", "evidence", "work", CARD):
        directory /= part
        if part == component:
            directory.symlink_to(destination, target_is_directory=True)
            break
        directory.mkdir(mode=0o700)
    assert execute(source).returncode != 0
    assert not list(destination.iterdir()) and not source[4].exists()


def test_existing_private_directory_preserves_other_evidence(source):
    directory = source[3] / f".skcapstone/evidence/work/{CARD}"
    directory.mkdir(parents=True, mode=0o700)
    previous = directory / "previous-report.md"
    previous.write_text("preserve history")
    assert execute(source).returncode == 0
    assert previous.read_text() == "preserve history"


@pytest.mark.parametrize(
    "problem",
    [
        "dirty",
        "untracked",
        "missing",
        "symlink",
        "detached",
        "main",
        "owner",
        "claim",
        "worktree",
        "unrelated_base",
    ],
)
def test_handoff_refuses_before_any_board_write(source, problem, tmp_path):
    root, evidence, _, _, log, environment = source
    if problem == "dirty":
        evidence.write_text("uncommitted changes\n")
    elif problem == "untracked":
        git(root, "rm", "--cached", str(evidence.relative_to(root)))
        git(root, "commit", "-m", "untrack evidence")
    elif problem == "missing":
        git(root, "rm", str(evidence.relative_to(root)))
        git(root, "commit", "-m", "remove evidence")
    elif problem == "symlink":
        evidence.unlink()
        evidence.symlink_to(root / "source.py")
        git(root, "add", ".")
        git(root, "commit", "-m", "symlink evidence")
    elif problem == "detached":
        git(root, "checkout", "--detach")
    elif problem == "main":
        git(root, "branch", "-m", "main")
    elif problem == "owner":
        environment["TEST_OWNER"] = "other-worker"
    elif problem == "claim":
        environment["TEST_CLAIM"] = "b" * 32
    elif problem == "worktree":
        destination = tmp_path / "linked-worktree"
        git(root, "worktree", "add", "-b", "linked", str(destination))
        source = (destination, *source[1:])
    elif problem == "unrelated_base":
        git(root, "checkout", "--orphan", "unrelated")
        git(root, "commit", "-m", "unrelated candidate")
    assert execute(source).returncode != 0
    assert not log.exists()


@pytest.mark.parametrize(
    "field,value",
    [("card_id", "../../card"), ("owner", "x;echo injected"), ("claim_revision", "bad")],
)
def test_identity_cannot_inject_completion_commands(field, value):
    values = {
        "card_id": CARD,
        "owner": OWNER,
        "claim_revision": CLAIM,
        "base_revision": "b" * 40,
        field: value,
    }
    with pytest.raises(ValueError):
        production_completion_recipe(**values)


def test_small_fixed_prompt_preserves_card_authorization_and_criteria(source):
    text = prompt(source)
    assert len(text) < 8000
    assert "Local commit authorized; no push." in text
    assert "1. Actual required fixture checks pass" in text
    assert "git push" not in text and "coord complete" not in text
    assert "Record a typed verdict, not only a verdict link" in text
    assert "no new commit is required or authorized" in text
    assert "actual existing HEAD tree" in text and "--candidate <private report path>" in text
