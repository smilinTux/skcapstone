"""Released modern producer claims retain proof, never invented ownership."""

import json

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet import production_review_custody as custody
from skcapstone.fleet import review_dispatch
from tests.fleet import remote_review_fixtures, test_remote_review_policy, test_source_bundle

strict_unit_terminal = custody.unit_terminal
execution = remote_review_fixtures.execution
remote_policy = test_remote_review_policy.remote_policy
source = test_source_bundle.source


@pytest.fixture
def released(execution, monkeypatch):
    e = execution
    s = e["source"]
    request_path = e["paths"].root / "dispatch/node-chiap02" / (s["card"] + ".json")
    request = json.loads(request_path.read_text())
    request.update(repository=s["remote"], base_revision=s["base"])
    request_path.write_text(json.dumps(request))
    e["bound_before"] = review_dispatch._source(e["home"], s["store"].fold(e["card"]))
    s["store"].append_event(
        s["card"],
        "release_claim",
        "operator",
        released_owner=s["owner"],
        expected_claim_revision=s["claim"],
        abandon_reason="error",
    )
    path = e["paths"].root / "status/node-chiap02/dispatch" / (s["card"] + ".json")
    status = json.loads(path.read_text())
    status.update(
        state="blocked", claim_released=True, error="claim released by authority before evidence"
    )
    status.pop("source_artifact")
    path.write_text(json.dumps(status))
    e["producer_status"] = status
    e["producer_path"] = path
    e["queries"] = []

    def terminal(unit, invocation, *, host):
        e["queries"].append((unit, invocation, host))
        return dict(LoadState="not-found", ActiveState="inactive", MainPID="0", ControlPID="0")

    monkeypatch.setattr(custody, "unit_terminal", terminal)
    return e


def test_exact_released_modern_source_is_reviewable_without_reclaim(released):
    e = released
    s = e["source"]
    result = review_dispatch._source(e["home"], s["store"].fold(e["card"]))
    assert result == e["bound_before"]
    assert s["store"].fold(s["card"]).owner is None
    assert e["queries"] == [(e["producer_status"]["unit"], "b" * 32, "chiap02")]


def test_acceptance_reuses_same_released_terminal_proof(released):
    e = released
    s = e["source"]
    manifest = json.loads(
        (
            e["home"] / "evidence/work" / s["card"] / "source-bundles" / (s["head"] + ".json")
        ).read_text()
    )
    terminal = acceptance._producer_terminal(e["home"], s["store"].fold(s["card"]), manifest)
    assert terminal["family"] == "glm"
    assert terminal["invocation"] == "b" * 32
    assert e["queries"]


@pytest.mark.parametrize(
    "change",
    [
        "new-claim",
        "new-outcome",
        "criteria",
        "no-release",
        "manifest",
        "bundle",
        "evidence",
        "failed-exit",
        "status-owner",
        "request",
        "unit",
        "active-host",
        "writer",
        "hold",
        "artifact",
        "claimed",
        "no-terminal-proof",
        "recipe-drift",
    ],
)
def test_released_source_failures_never_become_legacy_or_current_custody(
    released, monkeypatch, change
):
    e = released
    s = e["source"]
    st = s["store"]
    parent = s["card"]
    if change == "new-claim":
        st.append_event(parent, "claim", "new-builder", owner="new-builder")
        row = st.fold(parent)
        st.append_event(
            parent,
            "release_claim",
            "operator",
            released_owner=row.owner,
            expected_claim_revision=row.meta["_claim_revision"],
            abandon_reason="error",
        )
    elif change == "new-outcome":
        st.append_event(parent, "link", "operator", link_key="verdict", link_value="BLOCKED")
    elif change == "criteria":
        st.append_event(parent, "describe", "operator", description="changed product contract")
    elif change == "no-release":
        original = type(st)._read_events
        monkeypatch.setattr(
            type(st),
            "_read_events",
            lambda self, cid: [
                v for v in original(self, cid) if cid != parent or v["action"] != "release_claim"
            ],
        )
    elif change in ("manifest", "bundle", "evidence"):
        root = e["home"] / "evidence/work" / parent / "source-bundles"
        manifest = json.loads((root / (s["head"] + ".json")).read_text())
        path = (
            root / (s["head"] + ".json")
            if change == "manifest"
            else (
                root / (manifest["bundle_sha256"] + ".bundle")
                if change == "bundle"
                else root / (manifest["evidence_sha256"] + ".md")
            )
        )
        path.write_text("{}")
    elif change == "writer":
        e["producer_status"]["writer"]["role"] = "scheduler"
    elif change == "hold":
        st.append_event(parent, "add_label", "operator", label="hold")
    elif change == "claimed":
        st.append_event(parent, "claim", "new-builder", owner="new-builder")
    elif change == "recipe-drift":
        st.append_event(
            parent, "link", "operator", link_key="test_profile_recipe", link_value="changed"
        )
    elif change == "no-terminal-proof":
        e["producer_status"]["exit_code"] = None
        e["producer_status"].pop("terminal_proof", None)
    elif change == "artifact":
        e["producer_status"]["source_artifact"] = {"head": "0" * 40}
    elif change == "failed-exit":
        e["producer_status"]["exit_code"] = 1
    elif change == "status-owner":
        e["producer_status"]["owner"] = "another-owner"
    elif change == "request":
        e["producer_status"]["request_id"] = "0" * 64
    elif change == "unit":
        e["producer_status"]["unit"] = "skfleet-builder-wrong.service"
    else:

        def refuse(*a, **kw):
            raise ValueError("exact managed worker death unproven")

        monkeypatch.setattr(custody, "unit_terminal", refuse)
    e["producer_path"].write_text(json.dumps(e["producer_status"]))
    with pytest.raises(ValueError):
        review_dispatch._source(e["home"], st.fold(e["card"]))


def test_fresh_remote_review_of_released_source_reaches_acceptance(released, monkeypatch, request):
    e = remote_review_fixtures.terminal_review.__wrapped__(released, monkeypatch, request)
    s = e["source"]
    context = acceptance.collect(
        e["home"],
        e["request"]["policy"],
        e["card"],
        e["status"]["claim_revision"],
        process_check=lambda card: {"sessions": [], "units": []},
    )
    assert context["source"]["owner"] == s["owner"]
    assert context["source"]["claim"] == s["claim"]
    assert context["source"]["head"] == s["head"]
    assert s["store"].fold(s["card"]).owner is None


def test_qualified_terminal_without_exit_code_remains_supported(released):
    e = released
    e["producer_status"].update(exit_code=None, terminal_proof="qualified-terminal")
    e["producer_path"].write_text(json.dumps(e["producer_status"]))
    assert (
        review_dispatch._source(e["home"], e["source"]["store"].fold(e["card"]))
        == e["bound_before"]
    )


@pytest.mark.parametrize("observation", ["unreachable", "live", "different-invocation"])
def test_own_host_terminal_probe_refuses_unproven_death(released, monkeypatch, observation):
    import subprocess

    seen = []

    def run(command, **kwargs):
        seen.append(command)
        values = dict(
            LoadState="loaded",
            ActiveState="active" if observation == "live" else "failed",
            SubState="running" if observation == "live" else "failed",
            MainPID="123" if observation == "live" else "0",
            ControlPID="0",
            InvocationID="d" * 32 if observation == "different-invocation" else "b" * 32,
        )
        return subprocess.CompletedProcess(
            command,
            255 if observation == "unreachable" else 0,
            "\n".join(f"{k}={v}" for k, v in values.items()),
            "",
        )

    monkeypatch.setattr(custody, "unit_terminal", strict_unit_terminal)
    monkeypatch.setattr(custody.socket, "gethostname", lambda: "authority")
    monkeypatch.setattr(custody.subprocess, "run", run)
    with pytest.raises(ValueError, match="exact managed worker death unproven"):
        review_dispatch._source(
            released["home"], released["source"]["store"].fold(released["card"])
        )
    assert seen[0][:5] == [
        "ssh",
        "-oBatchMode=yes",
        "-oStrictHostKeyChecking=yes",
        "-oConnectTimeout=5",
        "chiap02",
    ]
