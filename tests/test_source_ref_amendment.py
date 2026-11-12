"""Only attributed operator ref amendments can retain an immutable source pin."""

from datetime import datetime, timedelta, timezone

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import source_ref_amendment as amendment

CARD = "ca230927"
BASE = "a" * 40
REF = "preserve/w7-ready-f6deb79"


@pytest.fixture
def binding(tmp_path, monkeypatch):
    created = datetime.now(timezone.utc) - timedelta(days=1)
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id=CARD,
            kind="task",
            title="[M] W7 source",
            created_by="test",
            created_at=created.isoformat(),
            meta={
                "repository": "https://skgit.skstack01.douno.it/smilinTux/sklegal.git",
                "base_ref": "work/old",
                "base_revision": BASE,
            },
        )
    )
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    probes = []
    monkeypatch.setattr(amendment, "prove_ref", lambda *args: probes.append(args))

    def link(writer="lumina-nor", timestamp=None, **extra):
        if extra:
            for key, value in extra.items():
                store.append_event(CARD, "link", writer, link_key=key, link_value=value)
        kwargs = {"ts": timestamp} if timestamp else {}
        store.append_event(CARD, "link", writer, link_key="base_ref", link_value=REF, **kwargs)
        card = store.fold(CARD)
        return {"id": CARD, "meta": card.meta, "links": card.links}

    return store, link, probes, created


def test_operator_amendment_proves_unchanged_source_and_logs(binding):
    _, link, probes, _ = binding
    core = link(
        repository="https://skgit.skstack01.douno.it/smilinTux/sklegal.git", base_revision=BASE
    )
    logs = []
    assert amendment.amended_source_ref(core, emit=logs.append) == REF
    assert probes == [(core["meta"]["repository"], REF, BASE)]
    assert logs == [f"SOURCE_BINDING_AMENDED|{CARD}|work/old|{REF}|lumina-nor"]


def test_absent_identity_links_inherit_immutable_metadata(binding):
    _, link, _, _ = binding
    assert amendment.amended_source_ref(link()) == REF


@pytest.mark.parametrize(
    "writer", ["pi-glm-chiap08-ca230927", "codex-worker-ca230927", "niobe", "unknown"]
)
def test_worker_or_nonoperator_link_refused(binding, writer):
    _, link, probes, _ = binding
    with pytest.raises(ValueError, match="operator"):
        amendment.amended_source_ref(link(writer))
    assert not probes


@pytest.mark.parametrize(
    "field,value", [("repository", "https://other.test/r.git"), ("base_revision", "b" * 40)]
)
def test_repository_or_revision_change_refused(binding, field, value):
    _, link, probes, _ = binding
    with pytest.raises(ValueError, match="unchanged"):
        amendment.amended_source_ref(link(**{field: value}))
    assert not probes


def test_old_or_future_link_refused(binding):
    _, link, probes, created = binding
    for timestamp in [
        created.isoformat(),
        (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
    ]:
        with pytest.raises(ValueError, match="timestamp"):
            amendment.amended_source_ref(link(timestamp=timestamp))
    assert not probes


def test_later_worker_reassertion_cannot_borrow_operator_provenance(binding):
    _, link, probes, _ = binding
    link()
    with pytest.raises(ValueError, match="operator"):
        amendment.amended_source_ref(link("pi-glm-chiap08-ca230927"))
    assert not probes


def test_unreachable_ref_refused_without_success_log(binding, monkeypatch):
    _, link, _, _ = binding

    def unreachable(*_args):
        raise ValueError("amended ref is unreachable")

    monkeypatch.setattr(amendment, "prove_ref", unreachable)
    logs = []
    with pytest.raises(ValueError, match="unreachable"):
        amendment.amended_source_ref(link(), emit=logs.append)
    assert logs == []


def test_native_card_must_match_supplied_projection(binding):
    _, link, probes, _ = binding
    core = link()
    core["meta"] = dict(core["meta"], base_ref="invented")
    with pytest.raises(ValueError, match="native"):
        amendment.amended_source_ref(core)
    assert not probes


def test_rotation_and_shared_validators_accept_same_proven_amendment(binding):
    from skcapstone.fleet.builder_dispatch import _source
    from skcapstone.fleet.source_bundle import _binding
    from tests.test_skfleet_workspace_materialization import _helpers

    _, link, _, _ = binding
    core = link()
    ns = _helpers()
    ns.update(HOME="/unused", d="/unused", log=lambda *_args: None)
    expected = (core["meta"]["repository"], REF, BASE)
    assert ns["_source_workspace_spec"](core, []) == expected
    assert _source(core) == expected
    assert _binding(core, "base_ref") == REF


def test_coord_overlay_event_proves_operator_amendment(binding, tmp_path):
    from skcoord.card import CardEvent, CardEventLog

    _, _, probes, _ = binding
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id=CARD,
            action="link",
            writer="lumina-nor",
            link_key="base_ref",
            link_value=REF,
        )
    )
    card = CardStore(tmp_path).fold(CARD)
    assert (
        amendment.amended_source_ref({"id": CARD, "meta": card.meta, "links": card.links}) == REF
    )
    assert len(probes) == 1


def test_concurrent_link_change_during_fetch_refused(binding, monkeypatch):
    _, link, _, _ = binding
    core = link()
    monkeypatch.setattr(amendment, "prove_ref", lambda *_: link("pi-glm-worker"))
    with pytest.raises(ValueError, match="operator"):
        amendment.amended_source_ref(core)


@pytest.fixture
def git_source(tmp_path, monkeypatch):
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    real_run = subprocess.run

    def git(*args):
        return real_run(
            ["/usr/bin/git", *args],
            cwd=source,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.test")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "descendant")
    git("branch", REF)
    git("checkout", "--orphan", "unrelated")
    git("commit", "--allow-empty", "-m", "unrelated")
    remote = "https://example.test/source.git"
    calls = []

    def local_transport(command, **kwargs):
        calls.append((command, kwargs))
        # Exercise real Git against a local fixture, replacing only the transport URL.
        return real_run([str(source) if arg == remote else arg for arg in command], **kwargs)

    monkeypatch.setattr(amendment.subprocess, "run", local_transport)
    return remote, base, calls, git


def test_real_fetch_proves_revision_ancestry_with_sanitized_environment(git_source):
    remote, base, calls, _ = git_source
    amendment.prove_ref(remote, REF, base)
    assert any("fetch" in cmd for cmd, _ in calls)
    assert any("merge-base" in cmd for cmd, _ in calls)
    for cmd, kwargs in calls:
        assert "credential.helper=" in cmd
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["HOME"] == "/nonexistent"


@pytest.mark.parametrize("ref", ["unrelated", "missing"])
def test_real_fetch_refuses_unreachable_revision_or_missing_ref(git_source, ref):
    remote, base, _, _ = git_source
    with pytest.raises(ValueError, match="proof failed"):
        amendment.prove_ref(remote, ref, base)


def test_ambiguous_branch_and_tag_refused(git_source):
    remote, base, _, git = git_source
    git("tag", REF)
    with pytest.raises(ValueError, match="ambiguous"):
        amendment.prove_ref(remote, REF, base)


def test_credential_bearing_repository_refused_before_probe(git_source):
    _, base, calls, _ = git_source
    with pytest.raises(ValueError, match="credential-free"):
        amendment.prove_ref("https://secret@example.test/source.git", REF, base)
    assert calls == []


def test_fetch_timeout_refuses(git_source, monkeypatch):
    import subprocess

    remote, base, _, _ = git_source

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 30)

    monkeypatch.setattr(amendment.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="proof unavailable"):
        amendment.prove_ref(remote, REF, base)


def test_ref_moving_between_advertisement_and_fetch_refuses(git_source, monkeypatch):
    remote, base, _, _ = git_source
    run = amendment.subprocess.run

    def changed(command, **kwargs):
        result = run(command, **kwargs)
        if "ls-remote" in command:
            result.stdout = f"{'b' * 40}\trefs/heads/{REF}\n"
        return result

    monkeypatch.setattr(amendment.subprocess, "run", changed)
    with pytest.raises(ValueError, match="changed during fetch"):
        amendment.prove_ref(remote, REF, base)


def test_dispatch_validator_uses_explicit_coordination_home(binding, tmp_path, monkeypatch):
    from skcapstone.fleet.builder_dispatch import _source

    _, link, _, _ = binding
    core = link()
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "other-home"))
    assert _source(core, home=tmp_path)[1] == REF
