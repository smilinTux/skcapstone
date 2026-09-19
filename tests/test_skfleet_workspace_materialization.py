"""Tests for source workspace materialization before fleet claims."""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(nodes) == set(names)

    # Module-level constants the loaded functions close over. Without these the
    # exec'd function bodies raise NameError on any module global, which would
    # otherwise push shared vocabulary (such as the safety label names) into
    # duplicated literals inside each function purely to satisfy this harness.
    # This only ADDS names to the namespace; no assertion above is relaxed.
    def _is_literal_constant(node: ast.stmt) -> bool:
        if not isinstance(node, ast.Assign):
            return False
        if not all(
            isinstance(target, ast.Name) and target.id.isupper() for target in node.targets
        ):
            return False
        try:
            ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            return False
        return True

    constants = [node for node in tree.body if _is_literal_constant(node)]
    namespace: dict[str, object] = {
        "Path": Path,
        "os": os,
        "re": re,
        "shutil": shutil,
        "subprocess": subprocess,
        "urlsplit": urlsplit,
    }
    exec(
        compile(
            ast.Module(constants + [nodes[name] for name in names], []),
            str(ROTATE),
            "exec",
        ),
        namespace,
    )
    return namespace


def _helpers() -> dict[str, object]:
    return _load(
        "_resolve_workspace_root",
        "_complete_source_binding",
        "_source_workspace_spec",
        "_normalize_credential_free_https_remote",
        "_select_matching_source_remote",
        "_verify_source_workspace",
        "_materialize_worker_workspace",
    )


def _remote_listing(remotes: dict[str, str]) -> str:
    """Format git config --get-regexp remote URL output."""
    return "".join(f"remote.{name}.url {url}\n" for name, url in remotes.items())


def _is_remote_listing(command: list[str]) -> bool:
    """Return True when command lists configured remote URLs."""
    return len(command) >= 2 and command[-2] == "--get-regexp"


def test_preclaim_source_ref_accepts_only_remote_exact_ref() -> None:
    preflight = _load("_preclaim_source_ref")["_preclaim_source_ref"]
    calls: list[list[str]] = []

    def present(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "abc123\trefs/heads/main\n", "")

    preflight(
        "https://github.com/smilinTux/sklegal",
        "refs/heads/main",
        "a" * 40,
        runner=present,
    )
    assert calls == [
        [
            "git",
            "ls-remote",
            "--exit-code",
            "https://github.com/smilinTux/sklegal",
            "refs/heads/main",
        ]
    ]


def test_preclaim_source_ref_blocks_before_workspace_creation() -> None:
    preflight = _load("_preclaim_source_ref")["_preclaim_source_ref"]

    def absent(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "not found")

    with pytest.raises(ValueError, match="reconstructability_blocked"):
        preflight("https://github.com/smilinTux/sklegal", "missing", absent)


def test_source_card_requires_exact_repository_and_base() -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="repository"):
        spec({"links": {}}, ["source-only"])
    with pytest.raises(ValueError, match="base_ref"):
        spec(
            {"links": {"repository": "https://github.com/smilinTux/sklegal"}},
            ["source-only"],
        )


@pytest.mark.parametrize(
    "repository",
    [
        "http://github.com/smilinTux/sklegal",
        "https://token@github.com/smilinTux/sklegal",
        "https://github.com/smilinTux/sklegal?token=secret",
    ],
)
def test_source_card_rejects_unsafe_repository_links(repository: str) -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="credential-free https"):
        spec(
            {"links": {"repository": repository, "base_ref": "main"}},
            ["source-only"],
        )


def test_legacy_sha_base_ref_normalizes_to_default_ref_and_exact_revision() -> None:
    spec = _helpers()["_source_workspace_spec"]
    revision = "a" * 40

    assert spec(
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": revision,
            },
            "meta": {"base_ref": "main", "base_revision": revision},
        },
        ["source-only"],
    ) == ("https://github.com/smilinTux/sklegal", "main", revision)


def test_legacy_sha_base_ref_rejects_conflicting_exact_revision() -> None:
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="conflicts"):
        spec(
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "a" * 40,
                },
                "meta": {"base_revision": "b" * 40},
            },
            ["source-only"],
        )


def test_exact_revision_is_checked_out_after_named_ref_clone(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    revision = "c" * 40
    calls: list[list[str]] = []

    def clone(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if _is_remote_listing(command):
            output = _remote_listing({"origin": "https://github.com/smilinTux/sklegal"})
        elif "status" in command or "fetch" in command or "checkout" in command:
            output = ""
        elif "rev-parse" in command:
            output = revision + "\n"
        else:
            output = "f" * 40 + "\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    assert materialize(
        str(target),
        {
            "meta": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": revision,
            }
        },
        ["source-only"],
        runner=clone,
    ) == str(target)
    checkout_call = [
        "git",
        "-C",
        str(target.with_name(".worker.materializing-" + str(os.getpid()))),
        "checkout",
        "--quiet",
        "--detach",
        revision,
    ]
    assert checkout_call in calls
    fetch_at = next(index for index, command in enumerate(calls) if "fetch" in command)
    assert fetch_at < calls.index(checkout_call)


def test_non_source_card_keeps_empty_working_directory(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    assert materialize(str(target), {}, []) == str(target)
    assert target.is_dir()


def test_source_checkout_is_cloned_atomically(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    target.mkdir()
    calls: list[list[str]] = []

    def clone(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if _is_remote_listing(command):
            return subprocess.CompletedProcess(
                command,
                0,
                _remote_listing({"origin": "https://github.com/smilinTux/sklegal"}),
                "",
            )
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": "a" * 40,
            }
        },
        ["source-only"],
        runner=clone,
    )
    assert result == str(target)
    assert (target / ".git").is_dir()
    assert calls[0][1:8] == [
        "clone",
        "--quiet",
        "--no-checkout",
        "--single-branch",
        "--branch",
        "main",
        "--",
    ]


def test_failed_clone_leaves_no_partial_workspace(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"

    def fail(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        checkout = Path(command[-1])
        checkout.mkdir()
        (checkout / "partial").write_text("partial", encoding="utf-8")
        return subprocess.CompletedProcess(command, 1, "", "denied")

    with pytest.raises(ValueError, match="denied"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=fail,
        )
    assert not target.exists()
    assert not list(tmp_path.glob(".*.materializing-*"))


def test_interrupted_clone_cleans_up_and_can_retry(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    core = {
        "links": {
            "repository": "https://github.com/smilinTux/sklegal",
            "base_ref": "main",
            "base_revision": "a" * 40,
        }
    }

    def interrupt(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).mkdir()
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        materialize(str(target), core, ["source-only"], runner=interrupt)
    assert not target.exists()
    assert not list(tmp_path.glob(".*.materializing-*"))

    def retry(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[1] == "clone":
            checkout = Path(command[-1])
            checkout.mkdir()
            (checkout / ".git").mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")
        if _is_remote_listing(command):
            output = _remote_listing({"origin": "https://github.com/smilinTux/sklegal"})
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    assert materialize(str(target), core, ["source-only"], runner=retry) == str(target)
    assert (target / ".git").is_dir()


def test_existing_dirty_workspace_is_preserved_and_rejected(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)

    def dirty(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if _is_remote_listing(command):
            output = _remote_listing({"origin": "https://github.com/smilinTux/sklegal"})
        elif "status" in command:
            output = " M preserved.py\n"
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ValueError, match="custody state"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=dirty,
        )
    assert target.is_dir()


def test_existing_clean_source_workspace_is_reused(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)

    def clean(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if _is_remote_listing(command):
            output = _remote_listing({"origin": "https://github.com/smilinTux/sklegal"})
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": "a" * 40,
            }
        },
        ["source-only"],
        runner=clean,
    )
    assert result == str(target)


def test_configured_source_workspace_is_still_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "configured"
    (target / ".git").mkdir(parents=True)
    monkeypatch.setenv("SKFLEET_WORKSPACE", str(target))

    def wrong_origin(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if _is_remote_listing(command):
            output = _remote_listing({"origin": "https://github.com/example/wrong"})
        elif "status" in command or "fetch" in command:
            output = ""
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    with pytest.raises(ValueError, match="does not match"):
        materialize(
            str(tmp_path / "unused"),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=wrong_origin,
        )


def test_matching_non_origin_remote_passes_repository_verification() -> None:
    """Accept the unique alias remote when origin is an unrelated mirror."""
    verify = _helpers()["_verify_source_workspace"]
    calls: list[list[str]] = []
    revision = "a" * 40

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if _is_remote_listing(command):
            return subprocess.CompletedProcess(
                command,
                0,
                _remote_listing(
                    {
                        "origin": "https://git.example.internal/sklegal.git",
                        "github": "https://github.com/smilinTux/sklegal.git",
                    }
                ),
                "",
            )
        if "status" in command or "fetch" in command or "merge-base" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "rev-parse" in command:
            return subprocess.CompletedProcess(command, 0, revision + "\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    verify(
        "/tmp/workspace",
        "https://github.com/smilinTux/sklegal",
        "main",
        revision,
        runner=runner,
    )
    assert [
        "git",
        "-C",
        "/tmp/workspace",
        "fetch",
        "--quiet",
        "github",
        "main",
    ] in calls


def test_remote_fetch_timeout_is_bounded_without_timing_out_local_git() -> None:
    verify = _helpers()["_verify_source_workspace"]
    observed: list[tuple[list[str], object]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append((command, kwargs.get("timeout")))
        if _is_remote_listing(command):
            return subprocess.CompletedProcess(
                command,
                0,
                _remote_listing({"origin": "https://github.com/smilinTux/sklegal"}),
                "",
            )
        if "fetch" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "", "")

    with pytest.raises(ValueError, match="reconstructability_blocked: workspace fetch timed out"):
        verify(
            "/tmp/workspace",
            "https://github.com/smilinTux/sklegal",
            "main",
            "a" * 40,
            runner=runner,
        )
    assert next(timeout for command, timeout in observed if "fetch" in command) <= 15
    assert all(timeout is None for command, timeout in observed if "fetch" not in command)


def test_remote_clone_timeout_is_bounded_and_cleans_temporary_workspace(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    observed_timeout: object = None

    def timeout(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal observed_timeout
        observed_timeout = kwargs.get("timeout")
        temporary = Path(command[-1])
        temporary.mkdir()
        (temporary / "partial").write_text("partial", encoding="utf-8")
        raise subprocess.TimeoutExpired(command, observed_timeout)

    with pytest.raises(ValueError, match="workspace materialization timed out"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=timeout,
        )
    assert observed_timeout <= 15
    assert not target.exists()
    assert not list(tmp_path.glob(".*.materializing-*"))


def test_ambiguous_matching_remotes_fail_closed() -> None:
    select = _helpers()["_select_matching_source_remote"]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            _remote_listing(
                {
                    "origin": "https://github.com/smilinTux/sklegal",
                    "github": "https://github.com/smilinTux/sklegal.git",
                }
            ),
            "",
        )

    with pytest.raises(ValueError, match="ambiguous"):
        select("/tmp/workspace", "https://github.com/smilinTux/sklegal", runner=runner)


def test_credential_bearing_matching_remote_fails_closed() -> None:
    select = _helpers()["_select_matching_source_remote"]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            _remote_listing(
                {
                    "origin": "https://mirror.example/sklegal",
                    "github": "https://token@github.com/smilinTux/sklegal",
                }
            ),
            "",
        )

    with pytest.raises(ValueError, match="credential-free"):
        select("/tmp/workspace", "https://github.com/smilinTux/sklegal", runner=runner)


def test_no_matching_remote_fails_closed() -> None:
    select = _helpers()["_select_matching_source_remote"]

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            _remote_listing({"origin": "https://github.com/example/wrong"}),
            "",
        )

    with pytest.raises(ValueError, match="does not match"):
        select("/tmp/workspace", "https://github.com/smilinTux/sklegal", runner=runner)


def test_materialization_precedes_claim_in_scheduler_source() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    preflight_at = source.index("_preclaim_source_ref(*_source_spec)")
    materialize_at = source.index("_materialize_worker_workspace(", preflight_at)
    claim_at = source.index("claim=subprocess.run(", materialize_at)
    assert preflight_at < materialize_at < claim_at
    assert "os.makedirs(workspace,exist_ok=True)" not in source


def _stale_workspace_runner(
    remotes: dict[str, str],
    stale: str,
    exact: str,
    anchors: str,
    issued: list[list[str]],
):
    """Simulate a clean workspace parked at a prior card's commit."""
    state = {"reset": False}

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        issued.append(list(command))
        if _is_remote_listing(command):
            return subprocess.CompletedProcess(command, 0, _remote_listing(remotes), "")
        if "status" in command or "fetch" in command or "merge-base" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "checkout" in command:
            state["reset"] = True
            return subprocess.CompletedProcess(command, 0, "", "")
        if "for-each-ref" in command:
            return subprocess.CompletedProcess(command, 0, anchors, "")
        if command[-1] == "HEAD^{commit}":
            head = exact if state["reset"] else stale
            return subprocess.CompletedProcess(command, 0, head + "\n", "")
        return subprocess.CompletedProcess(command, 0, exact + "\n", "")

    return runner


def test_stale_clean_workspace_is_reset_to_exact_base_revision(
    tmp_path: Path,
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)
    issued: list[list[str]] = []
    runner = _stale_workspace_runner(
        {"origin": "https://github.com/smilinTux/sklegal"},
        stale="b" * 40,
        exact="a" * 40,
        anchors="refs/heads/feat/prior-claim-work\n",
        issued=issued,
    )

    result = materialize(
        str(target),
        {
            "links": {
                "repository": "https://github.com/smilinTux/sklegal",
                "base_ref": "main",
                "base_revision": "a" * 40,
            }
        },
        ["source-only"],
        runner=runner,
    )
    assert result == str(target)
    resets = [c for c in issued if "checkout" in c and "a" * 40 in c]
    assert resets, "expected a detached checkout of the exact base_revision"
    assert "--detach" in resets[0]


def test_stale_unanchored_workspace_stays_blocked(tmp_path: Path) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "worker"
    (target / ".git").mkdir(parents=True)
    issued: list[list[str]] = []
    runner = _stale_workspace_runner(
        {"origin": "https://github.com/smilinTux/sklegal"},
        stale="b" * 40,
        exact="a" * 40,
        anchors="",
        issued=issued,
    )

    with pytest.raises(ValueError, match="not anchored"):
        materialize(
            str(target),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=runner,
        )
    assert not [c for c in issued if "checkout" in c]


def test_configured_stale_workspace_is_not_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    materialize = _helpers()["_materialize_worker_workspace"]
    target = tmp_path / "configured"
    (target / ".git").mkdir(parents=True)
    monkeypatch.setenv("SKFLEET_WORKSPACE", str(target))
    issued: list[list[str]] = []
    runner = _stale_workspace_runner(
        {"origin": "https://github.com/smilinTux/sklegal"},
        stale="b" * 40,
        exact="a" * 40,
        anchors="refs/heads/feat/prior-claim-work\n",
        issued=issued,
    )

    with pytest.raises(ValueError, match="does not match exact base_revision"):
        materialize(
            str(tmp_path / "unused"),
            {
                "links": {
                    "repository": "https://github.com/smilinTux/sklegal",
                    "base_ref": "main",
                    "base_revision": "a" * 40,
                }
            },
            ["source-only"],
            runner=runner,
        )
    assert not [c for c in issued if "checkout" in c]


# ---------------------------------------------------------------------------
# source-only carried two unrelated meanings at once (routing + safety).
# The tests below pin the split: routing now also fires on a complete binding,
# so the safety half can be expressed on its own without a card's binding
# silently going unchecked. See docs/fleet/source-only-split.md.
# ---------------------------------------------------------------------------


_COMPLETE_BINDING = {
    "repository": "https://github.com/smilinTux/skcapstone.git",
    "base_ref": "main",
    "base_revision": "b" * 40,
}


def test_complete_binding_without_the_label_is_still_routed() -> None:
    """A binding is honoured even when nobody applied ``source-only``.

    Measured on the chi board 2026-09-18 by folding every card in a fresh
    process: 79 live cards carried a complete repository/base_ref/base_revision
    binding and no ``source-only`` label, so the dispatcher never looked at the
    binding at all. All 79 validate cleanly through this function, which is why
    widening the trigger blocks nothing that dispatches today.
    """
    spec = _helpers()["_source_workspace_spec"]
    assert spec({"links": dict(_COMPLETE_BINDING)}, []) == (
        _COMPLETE_BINDING["repository"],
        "main",
        "b" * 40,
    )
    assert spec({"meta": dict(_COMPLETE_BINDING)}, []) == (
        _COMPLETE_BINDING["repository"],
        "main",
        "b" * 40,
    )


def test_partial_binding_without_the_label_does_not_jam_dispatch() -> None:
    """A partial binding and no label stays inert rather than blocking.

    This is deliberately NOT symmetric with the labelled path. 511 live chi
    cards carry a partial binding and no ``source-only`` label; raising on them
    would turn every one into a ``WORKSPACE_BLOCKED`` skip, trading a checking
    win for a fleet-wide liveness regression. A partial binding is a triage
    defect to be reported, not a dispatch trigger.
    """
    spec = _helpers()["_source_workspace_spec"]
    assert spec({"links": {"repository": _COMPLETE_BINDING["repository"]}}, []) is None
    assert spec({"links": {"base_revision": "c" * 40}}, []) is None
    assert (
        spec(
            {"links": {"repository": _COMPLETE_BINDING["repository"], "base_ref": "main"}},
            [],
        )
        is None
    )


def test_no_external_action_label_never_demands_a_binding() -> None:
    """The safety label is routing-inert, which is the whole point of the split.

    ``source-only`` forces a binding to exist (see
    ``test_source_card_requires_exact_repository_and_base``). A card whose only
    need is "take no external action" can therefore now say so without being
    forced to invent a repository it does not use, and without triage having to
    strip a safety constraint to unjam its routing.
    """
    spec = _helpers()["_source_workspace_spec"]
    assert spec({}, ["no-external-action"]) is None
    assert spec({"links": {}}, ["no-external-action"]) is None


def test_no_external_action_alongside_a_binding_still_routes() -> None:
    """Declaring safety does not waive a binding the card actually carries."""
    spec = _helpers()["_source_workspace_spec"]
    assert spec({"links": dict(_COMPLETE_BINDING)}, ["no-external-action"]) == (
        _COMPLETE_BINDING["repository"],
        "main",
        "b" * 40,
    )


def test_source_only_without_a_binding_still_fails_closed() -> None:
    """Regression fence: the legacy label keeps its strict contract.

    2,612 chi cards carry ``source-only`` and none of them are being relabelled
    by this change, so the labelled path must behave byte-for-byte as before.
    If this assertion is ever relaxed, those cards start dispatching without the
    pinned checkout they were authored against.
    """
    spec = _helpers()["_source_workspace_spec"]
    with pytest.raises(ValueError, match="repository"):
        spec({"links": {}}, ["source-only"])


def test_no_external_action_rail_reaches_the_worker_brief() -> None:
    """Both labels emit the constraint; an unlabelled card emits nothing."""
    rail = _load("_worker_no_external_action_instructions")[
        "_worker_no_external_action_instructions"
    ]
    assert rail([]) == ""
    assert "NO EXTERNAL ACTION" in rail(["no-external-action"])
    assert "NO EXTERNAL ACTION" in rail(["Source-Only"])
    assert "acceptance criteria" in rail(["source-only"])


def test_safety_label_names_match_the_library_constants() -> None:
    """The dispatcher is a separately deployed artifact, so pin the vocabulary.

    ``~/.local/bin/skfleet-rotate.py`` is copied per host and a git pull does
    not update it. If the script and the library ever disagree on the spelling
    of the safety label, a card would carry a constraint one side honours and
    the other ignores, which is exactly the failure this split exists to end.
    """
    from skcapstone.source_binding import NO_EXTERNAL_ACTION_LABELS

    source = ROTATE.read_text(encoding="utf-8")
    for label in NO_EXTERNAL_ACTION_LABELS:
        assert f'"{label}"' in source
