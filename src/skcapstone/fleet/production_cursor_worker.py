"""Trusted Cursor stream adapter and guarded producer handoff, outside the sandbox."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .evidence_io import native_command, native_state
from .production_cursor import candidate, require_model, runtime
from .production_cursor_sandbox import sandbox_command, write_config
from .source_bundle import MAX_EVIDENCE, _once, _read, _sha

RESULT = ".git/skfleet-cursor-result.json"
DISPLAY_MODELS = {"composer-2.5": "Composer 2.5", "cursor-grok-4.6-high": "Grok 4.6"}
MAX_LINE = 1024 * 1024
MAX_STREAM = 32 * 1024 * 1024


def progress_path(home: Path, owner: str, claim: str) -> Path:
    """Keep host-written progress outside the agent's writable source mount."""
    from .production_brief import _identity

    _identity("00000000", owner, claim)
    return home / "fleet/cursor-progress" / owner / (claim + ".json")


def read_progress(home: Path, owner: str, card: str, claim: str) -> dict | None:
    """Reject stale, redirected or differently attributed progress observations."""
    try:
        row = json.loads(_read(progress_path(home, owner, claim), 4096))
        if not isinstance(row, dict):
            return None
        if (row.get("owner"), row.get("card_id"), row.get("claim_revision")) != (
            owner,
            card,
            claim,
        ):
            return None
        if (
            type(row["bytes"]) is not int
            or not 0 < row["bytes"] <= MAX_STREAM
            or type(row["progress_at"]) not in (int, float)
            or not 0 < row["progress_at"] <= time.time()
        ):
            return None
        return row
    except (OSError, ValueError, TypeError, KeyError):
        return None


def consume_stream(stream, *, model: str, progress, output) -> None:
    """Translate bounded NDJSON activity; a clean result is necessary, never a verdict."""
    require_model(model)
    total, initialized, finished, session = 0, False, False, None
    while raw := stream.readline(MAX_LINE + 1):
        total += len(raw)
        if len(raw) > MAX_LINE or total > MAX_STREAM or not raw.endswith(b"\n"):
            raise ValueError("Cursor stream exceeds bounds or is incomplete")
        row = json.loads(raw)
        if not isinstance(row, dict) or finished:
            raise ValueError("Cursor stream contains invalid or post-result data")
        output.write(raw)
        output.flush()
        kind = row.get("type")
        if row.get("is_error") or kind == "error":
            raise ValueError("Cursor reported an error")
        reported = row.get("model")
        if reported is not None and reported not in {model, DISPLAY_MODELS[model]}:
            raise ValueError("Cursor reported a different model")
        if kind == "system" and row.get("subtype") == "init":
            if initialized or not reported or not isinstance(row.get("session_id"), str):
                raise ValueError("Cursor init is missing or ambiguous")
            initialized, session = True, row["session_id"]
        elif not initialized or row.get("session_id") != session:
            raise ValueError("Cursor activity lacks its pinned init/session")
        elif kind == "assistant" or (
            kind == "tool_call" and row.get("subtype") in {"started", "completed"}
        ):
            progress(total)
        elif kind == "result":
            if row.get("subtype") != "success" or row.get("is_error") is not False:
                raise ValueError("Cursor result is not successful")
            finished = True
        elif kind not in {"user", "thinking"}:
            raise ValueError("Cursor stream contains an unsupported event")
    if not initialized or not finished:
        raise ValueError("Cursor stream ended without a successful result")


def require_claim(home: Path, card: str, owner: str, claim: str) -> dict:
    """Read through coord and refuse superseded, terminal or conflicting custody."""
    core = native_command(home, ["show", card, "--json"])
    if (
        core.get("id") != card
        or core.get("owner") != owner
        or core.get("meta", {}).get("_claim_revision") != claim
        or core.get("status") != "doing"
        or core.get("archived")
        or core.get("meta", {}).get("claim_conflicts")
    ):
        raise ValueError("Cursor native claim changed")
    return core


def git(workspace: Path, *args: str) -> bytes:
    """Inspect only the isolated Git database without executing repository helpers."""
    if (workspace / ".git").is_symlink() or not (workspace / ".git").is_dir():
        raise ValueError("Cursor repository metadata is redirected")
    result = subprocess.run(
        [
            "/usr/bin/git",
            "-C",
            str(workspace),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_OPTIONAL_LOCKS": "0",
        },
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout


def source_matches(workspace: Path, request: dict, core: dict) -> None:
    """Recheck exact source binding before inference and before publishing a verdict."""
    from .builder_dispatch import _source

    if _source(core) != tuple(request[k] for k in ("repository", "base_ref", "base_revision")):
        raise ValueError("Cursor native source binding changed")
    if (
        git(workspace, "rev-parse", "--show-toplevel").decode().strip() != str(workspace)
        or git(workspace, "remote", "get-url", "origin").decode().strip() != request["repository"]
    ):
        raise ValueError("Cursor workspace source differs")
    git(workspace, "merge-base", "--is-ancestor", request["base_revision"], "HEAD")


def handoff(
    home: Path, request: dict, workspace: Path, owner: str, claim: str, revision: str
) -> None:
    """Relay an explicit report with exact revision guards through native coord only."""
    from ..blocked_verdict import validate_blocked_verdict

    core = require_claim(home, request["card_id"], owner, claim)
    source_matches(workspace, request, core)
    result = json.loads(_read(workspace / RESULT, 8192))
    if (
        not isinstance(result, dict)
        or set(result) != {"verdict", "claim_revision"}
        or result["claim_revision"] != claim
        or not isinstance(result["verdict"], str)
    ):
        raise ValueError("Cursor handoff has invalid fields or claim")
    verdict = result["verdict"]
    if verdict != "PASS_FOR_REVIEW" and not verdict.startswith("BLOCKED "):
        raise ValueError("Cursor cannot record terminal PASS or arbitrary lifecycle actions")
    validate_blocked_verdict("verdict", verdict)
    report = f"docs/evidence/agents/{request['card_id']}/COMPLETION-EVIDENCE.md"
    evidence = _read(workspace / report, MAX_EVIDENCE)
    head = git(workspace, "rev-parse", "HEAD").decode().strip()
    tree = git(workspace, "rev-parse", "HEAD^{tree}").decode().strip()
    ref = git(workspace, "symbolic-ref", "HEAD").decode().strip()
    if ref in {"refs/heads/main", "refs/heads/master"}:
        raise ValueError("Cursor handoff cannot use a publication branch")
    if verdict == "PASS_FOR_REVIEW":
        if (
            git(workspace, "status", "--porcelain")
            or git(workspace, "cat-file", "blob", "HEAD:" + report) != evidence
        ):
            raise ValueError("Cursor PASS requires a clean, committed exact report")
    report_path = (
        home / "evidence/work" / request["card_id"] / ("cursor-" + _sha(evidence) + ".md")
    )
    _once(report_path, evidence)
    transition = _sha(
        json.dumps(
            [request["card_id"], owner, claim, revision, verdict, head, tree, ref, _sha(evidence)],
            separators=(",", ":"),
        ).encode()
    )
    native_command(
        home,
        [
            "verdict",
            request["card_id"],
            verdict,
            "--candidate",
            str(report_path),
            "--commit",
            head,
            "--tree",
            tree,
            "--ref",
            ref,
            "--agent",
            owner,
            "--expected-source-revision",
            revision,
            "--expected-claim-revision",
            claim,
            "--transition-id",
            transition,
            "--expected-candidate-sha256",
            _sha(evidence),
        ],
    )
    # Typed verdict folds are not string links: verify the actual event, not CLI prose.
    from skcoord.card_store import CardStore

    events = CardStore(home)._read_events(request["card_id"])
    matches = [e for e in events if e.get("transition_id") == transition]
    if (
        len(matches) != 1
        or matches[0].get("verdict") != verdict
        or matches[0].get("candidate_sha256") != _sha(evidence)
        or matches[0].get("candidate_commit") != head
    ):
        raise ValueError("Cursor verdict did not read back")


def run(request: dict, workspace: Path) -> int:
    """Execute one admitted producer without exposing the board to Cursor tools."""
    home = Path(os.environ["SKCAPSTONE_HOME"])
    owner, claim = os.environ["SKAGENT"], os.environ["SKFLEET_CLAIM_REVISION"]
    card, bound = request["card_id"], request["production"]
    model = require_model(bound["model"])
    core = require_claim(home, card, owner, claim)
    from .production_builder import digest, policy

    current_policy = policy()
    if current_policy is None or digest(current_policy) != bound.get("policy_sha256"):
        raise ValueError("Cursor worker requires current production policy")
    expected = candidate(
        current_policy, request["logical_route"], core["labels"], host=bound["host"]
    )
    if not expected or any(bound.get(k) != v for k, v in expected.items()):
        raise ValueError("Cursor launch policy or card scope changed")
    source_matches(workspace, request, core)
    revision = native_state(home, card)["revision"]
    if (workspace / RESULT).exists() or (workspace / RESULT).is_symlink():
        raise ValueError("Cursor result already exists; reconcile its prior handoff before retry")
    prompt = (
        "Implement only this exact claimed source card in /work. The host owns coordination; "
        "do not run coord, skmail, other agents, model APIs, deployment or publication. "
        "Do not edit agent configuration or .git configuration. Preserve existing work. "
        "Use a named feature branch; create one if HEAD is detached. Commit only when the "
        "card expressly authorizes it; otherwise report BLOCKED. Run relevant local tests "
        "and write a truthful report at "
        f"docs/evidence/agents/{card}/COMPLETION-EVIDENCE.md on that non-main branch. "
        "Independent host qualification and review follow. If blocked, write that report "
        "without claiming tests passed. Then write "
        + RESULT
        + " with exactly "
        + json.dumps({"verdict": "PASS_FOR_REVIEW", "claim_revision": claim})
        + "; or replace verdict with BLOCKED blocked_on=<dependency|card|human|capability> "
        "referent=<exact referent> <reason>, and the verdict text must also carry "
        "attempted=<what you tried>, resume=<what the next agent does first> and "
        "work_location=<branch, commit, bundle, PR or none>. No terminal PASS. Card: "
        + json.dumps(core)
    )
    executable, auth = runtime()
    path = progress_path(home, owner, claim)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def progress(count):
        """Publish observed activity separately from the wrapper's heartbeat."""
        payload = dict(
            owner=owner, card_id=card, claim_revision=claim, progress_at=time.time(), bytes=count
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload))
        temporary.chmod(0o600)
        temporary.replace(path)

    with tempfile.TemporaryDirectory(prefix="skfleet-cursor-") as directory:
        config = Path(directory)
        write_config(config, model)
        argv = sandbox_command(
            workspace,
            executable,
            auth,
            config,
            model=model,
            owner=owner,
            card=card,
            claim=claim,
            prompt=prompt,
        )
        with subprocess.Popen(argv, stdout=subprocess.PIPE) as child:
            try:
                consume_stream(
                    child.stdout, model=model, progress=progress, output=sys.stdout.buffer
                )
                if child.wait() != 0:
                    raise ValueError("Cursor child exited unsuccessfully")
            except BaseException:
                child.kill()
                child.wait()
                raise
    handoff(home, request, workspace, owner, claim, revision)
    return 0


def main() -> int:
    """Parse only trusted wrapper arguments; errors fail the managed worker closed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    try:
        return run(json.loads(args.request), args.workspace)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print("Cursor worker refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
