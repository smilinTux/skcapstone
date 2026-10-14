"""Exact source preservation refuses changed bindings and conflicting refs."""

import copy
import subprocess

import pytest

from skcapstone.fleet import preserve_source as ps

SHA = "a" * 40
CARD = {
    "id": "1234abcd",
    "status": "ready",
    "owner": None,
    "archived": False,
    "meta": {
        "repository": "https://skgit.skstack01.douno.it/smilinTux/sklegal.git",
        "base_ref": "lost",
        "base_revision": SHA,
    },
    "links": {},
}


def test_preserves_then_proves_then_links_and_reads_back(tmp_path, monkeypatch):
    current = copy.deepcopy(CARD)
    calls = []

    def coord(home, *args):
        calls.append(args[0])
        if args[0] == "link":
            assert args[2] == "base_ref" and args[-1] == "lumina-nor"
            current["links"]["base_ref"] = args[3]
            return ""
        return copy.deepcopy(current)

    monkeypatch.setattr(ps, "_coord", coord)
    monkeypatch.setattr(ps, "find_source", lambda *a: tmp_path)
    monkeypatch.setattr(ps, "publish", lambda *a: calls.append("publish"))
    monkeypatch.setattr(ps, "prove_ref", lambda *a: calls.append("prove"))
    result = ps.preserve(tmp_path, CARD["id"], "lumina-nor", [tmp_path])
    assert result["base_ref"] == "preserve/1234abcd-aaaaaaaa"
    assert calls.index("publish") < calls.index("prove") < calls.index("link")
    assert calls[-1] == "prove"
    assert current["meta"] == CARD["meta"]


@pytest.mark.parametrize("change", ["owner", "revision", "repository", "archived"])
def test_changed_card_refuses_before_link(tmp_path, monkeypatch, change):
    current = copy.deepcopy(CARD)
    writes = []

    def coord(home, *args):
        if args[0] == "link":
            writes.append(args)
        return copy.deepcopy(current)

    def publish(*args):
        if change in ("revision", "repository"):
            current["meta"]["base_revision" if change == "revision" else change] = "changed"
        else:
            current[change] = True

    monkeypatch.setattr(ps, "_coord", coord)
    monkeypatch.setattr(ps, "find_source", lambda *a: tmp_path)
    monkeypatch.setattr(ps, "publish", publish)
    monkeypatch.setattr(ps, "prove_ref", lambda *a: None)
    with pytest.raises(ValueError):
        ps.preserve(tmp_path, CARD["id"], "lumina-nor", [tmp_path])
    assert not writes


def test_worker_refused_before_any_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_coord", lambda *a: pytest.fail("worker admitted"))
    with pytest.raises(ValueError, match="operator"):
        ps.preserve(tmp_path, CARD["id"], "pi-glm-1234abcd", [])


def test_failed_fetch_proof_never_links(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ps, "_coord", lambda h, *a: calls.append(a[0]) or copy.deepcopy(CARD))
    monkeypatch.setattr(ps, "find_source", lambda *a: tmp_path)
    monkeypatch.setattr(ps, "publish", lambda *a: None)

    def refuse(*args):
        raise ValueError("unreachable")

    monkeypatch.setattr(ps, "prove_ref", refuse)
    with pytest.raises(ValueError, match="unreachable"):
        ps.preserve(tmp_path, CARD["id"], "lumina-nor", [])
    assert "link" not in calls


def git(*args):
    return subprocess.check_output(["git", *map(str, args)], text=True).strip()


def test_real_publish_is_idempotent_and_refuses_overwrite(tmp_path):
    source, remote = tmp_path / "source", tmp_path / "remote.git"
    git("init", "-q", source)
    git(
        "-C",
        source,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "first",
    )
    first = git("-C", source, "rev-parse", "HEAD")
    git(
        "-C",
        source,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "second",
    )
    second = git("-C", source, "rev-parse", "HEAD")
    git("init", "--bare", "-q", remote)
    ref = "refs/heads/preserve/1234abcd-" + first[:8]
    ps.publish(source, str(remote), ref, first)
    ps.publish(source, str(remote), ref, first)
    with pytest.raises(ValueError, match="conflict"):
        ps.publish(source, str(remote), ref, second)
    assert git("--git-dir", remote, "rev-parse", ref) == first


def test_missing_source_refuses(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        ps.find_source([tmp_path], SHA)


def test_repository_amendment_refused():
    card = copy.deepcopy(CARD)
    card["links"]["repository"] = "https://skgit.skstack01.douno.it/smilinTux/other.git"
    with pytest.raises(ValueError, match="repository"):
        ps.binding(card)


def test_concurrent_existing_ancestor_cannot_be_fast_forwarded(tmp_path, monkeypatch):
    source, remote = tmp_path / "source", tmp_path / "remote.git"
    git("init", "-q", source)
    for message in ("first", "second"):
        git(
            "-C",
            source,
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.test",
            "commit",
            "--allow-empty",
            "-qm",
            message,
        )
    first = git("-C", source, "rev-parse", "HEAD^")
    second = git("-C", source, "rev-parse", "HEAD")
    git("init", "--bare", "-q", remote)
    ref = "refs/heads/preserve/1234abcd-" + second[:8]
    original = ps._git
    raced = False

    def racing(*args, **kwargs):
        nonlocal raced
        result = original(*args, **kwargs)
        if args[0] == "ls-remote" and not raced:
            raced = True
            git("-C", source, "push", str(remote), first + ":" + ref)
        return result

    monkeypatch.setattr(ps, "_git", racing)
    with pytest.raises(ValueError, match="conflict"):
        ps.publish(source, str(remote), ref, second)
    assert git("--git-dir", remote, "rev-parse", ref) == first


def test_discovers_exact_commit_in_self_contained_bundle(tmp_path):
    source = tmp_path / "source"
    git("init", "-q", source)
    git(
        "-C",
        source,
        "-c",
        "user.name=T",
        "-c",
        "user.email=t@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "bundle",
    )
    sha = git("-C", source, "rev-parse", "HEAD")
    bundle = tmp_path / "candidate.bundle"
    git("-C", source, "bundle", "create", bundle, "--all")
    assert ps.find_source([bundle], sha) == bundle


def test_cli_refuses_worker_without_board_io():
    from click.testing import CliRunner

    from skcapstone.fleet.cli import fleet

    result = CliRunner().invoke(fleet, ["preserve-source", "1234abcd", "--agent", "pi-glm-test"])
    assert result.exit_code != 0
    assert "authorized operator" in result.output
