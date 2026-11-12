"""Candidate-only SSH custody works without broad shared evidence folders."""

import io
import json
import shutil
from types import SimpleNamespace

import pytest

from skcapstone.fleet import source_bundle as bundle
from skcapstone.fleet import source_transport as transport
from tests.fleet.test_source_bundle import publish
from tests.fleet.test_source_bundle import source as source_fixture

source = source_fixture
pytestmark = pytest.mark.host_systemd


def authority_copy(source, tmp_path):
    authority = tmp_path / "authority"
    shutil.copytree(source["home"], authority)
    shutil.rmtree(authority / "evidence")
    # Native typed candidate paths are absolute and identical between real
    # fleet hosts. The test substitutes only that public path for two roots.
    from skcoord.card_store import CardStore

    evidence = authority / "evidence/work" / source["card"] / "COMPLETION-EVIDENCE.md"
    CardStore(authority).append_event(
        source["card"],
        "verdict",
        source["owner"],
        **(source["outcome"] | {"candidate_path": str(evidence)}),
    )
    return authority, evidence


def test_candidate_packet_populates_authority_and_unsynced_reviewer(source, tmp_path, monkeypatch):
    result = publish(source)
    authority, evidence = authority_copy(source, tmp_path)
    packet = transport._packet(source["home"], source["card"], source["head"])
    digest = transport._retain(authority, packet, authoritative=True)
    assert digest == result["manifest_sha256"]
    assert evidence.read_bytes() == source["shared"].read_bytes()
    assert transport._retain(authority, packet, authoritative=True) == digest
    reviewer = tmp_path / "reviewer"
    reviewer.mkdir()
    monkeypatch.setattr(transport, "_authority", lambda: "control")
    monkeypatch.setattr(transport.socket, "gethostname", lambda: "unsynced-worker")
    monkeypatch.setattr(
        transport,
        "_rpc",
        lambda host, request: transport._packet(authority, request["card"], request["head"]),
    )
    monkeypatch.setenv("SKCAPSTONE_HOME", str(reviewer))
    assert bundle.verify_review_source(source["core"], source["remote"], source["head"])
    assert bundle.import_review_source(
        source["core"], source["remote"], source["head"], tmp_path / "review-work"
    )
    assert not (reviewer / "evidence/work" / source["card"] / "COMPLETION-EVIDENCE.md").exists()


@pytest.mark.parametrize(
    "changed", ["claim", "head", "tree", "ref", "evidence", "repository", "base", "verdict"]
)
def test_authority_refuses_unbound_packet_without_publishing(source, tmp_path, changed):
    publish(source)
    authority, evidence = authority_copy(source, tmp_path)
    packet = transport._packet(source["home"], source["card"], source["head"])
    manifest = packet["manifest"]
    if changed == "claim":
        manifest["claim_revision"] = "a" * 32
    elif changed in {"head", "tree"}:
        manifest[changed] = "f" * 40
    elif changed == "ref":
        manifest["ref"] = "refs/heads/wrong"
    elif changed == "evidence":
        packet["evidence"] = "YWJj"
    elif changed == "repository":
        manifest["repository"] = "https://example.invalid/wrong.git"
    elif changed == "base":
        manifest["base_revision"] = "a" * 40
    else:
        from skcoord.card_store import CardStore

        CardStore(authority).append_event(
            source["card"], "verdict", source["owner"], verdict="FAIL"
        )
    with pytest.raises(bundle.SourceBundleError):
        transport._retain(authority, packet, authoritative=True)
    assert not evidence.exists()
    assert not (evidence.parent / "source-bundles").exists()


def test_push_uses_exact_ack_and_ssh_transport_is_bounded(source, monkeypatch):
    result = publish(source)
    monkeypatch.setattr(transport, "_authority", lambda: "control")
    monkeypatch.setattr(transport.socket, "gethostname", lambda: "worker")
    captured = []

    def run(command, **kwargs):
        captured.append((command, kwargs))
        request = json.loads(kwargs["input"])
        assert request["packet"]["manifest"]["head"] == source["head"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"manifest_sha256": result["manifest_sha256"]}).encode(),
        )

    monkeypatch.setattr(transport.subprocess, "run", run)
    transport.push(source["home"], result)
    command, kwargs = captured[0]
    assert "-oStrictHostKeyChecking=yes" in command
    assert "-oBatchMode=yes" in command
    assert kwargs["capture_output"] is True and kwargs["timeout"] == 60
    assert source["head"] not in " ".join(command)
    monkeypatch.setattr(
        transport.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=b""),
    )
    with pytest.raises(bundle.SourceBundleError):
        transport.push(source["home"], result)


def test_artifact_pull_rejects_another_candidate(source, monkeypatch, tmp_path):
    publish(source)
    packet = transport._packet(source["home"], source["card"], source["head"])
    packet["manifest"]["head"] = "f" * 40
    monkeypatch.setattr(transport, "_authority", lambda: "control")
    monkeypatch.setattr(transport.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(transport, "_rpc", lambda *args: packet)
    with pytest.raises(bundle.SourceBundleError):
        transport.pull(tmp_path / "empty", source["card"], source["head"])
    assert not (tmp_path / "empty").exists()


@pytest.mark.parametrize("changed", [None, "authority", "card", "head", "action", "source-only"])
def test_fixed_receiver_qualifies_exact_native_source_before_returning_bytes(
    source, monkeypatch, tmp_path, changed
):
    publish(source)
    account = tmp_path / "receiver-account"
    account.mkdir()
    target = account / ".skcapstone"
    shutil.copytree(source["home"], target)
    from skcoord.card_store import CardStore

    if changed == "source-only":
        CardStore(target).append_event(
            source["card"], "remove_label", source["owner"], label="source-only"
        )
    request = {
        "authority": "control",
        "action": "get",
        "card": source["card"],
        "head": source["head"],
    }
    if changed == "authority":
        request["authority"] = "other"
    elif changed == "card":
        request["card"] = "../../private"
    elif changed == "head":
        request["head"] = "a" * 40
    elif changed == "action":
        request["action"] = "execute"
    monkeypatch.setattr(transport.Path, "home", lambda: account)
    monkeypatch.setattr(transport.socket, "gethostname", lambda: "control")
    output = io.StringIO()
    monkeypatch.setattr(
        transport.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
    )
    monkeypatch.setattr(transport.sys, "stdout", output)
    if changed is None:
        transport.serve()
        assert json.loads(output.getvalue())["manifest"]["head"] == source["head"]
    else:
        with pytest.raises(SystemExit) as exc:
            transport.serve()
        assert exc.value.code == 1
        assert output.getvalue() == ""
