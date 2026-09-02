"""Fail-closed source repository and card worktree admission."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from skcoord.card_store import CardStore

_SOURCE_LABEL = "source-only"
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")


class SourceWorktreeError(ValueError):
    """Source worktree policy was not satisfied."""


@dataclass(frozen=True)
class SourceSpec:
    repository: str
    base_ref: str
    checkout: Path
    remote: str


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)
    if result.returncode:
        raise SourceWorktreeError(
            "git %s failed for %s: %s"
            % (" ".join(args), checkout, (result.stderr or result.stdout).strip()[:200])
        )
    return result.stdout.strip()


def is_source_card(core: dict[str, Any]) -> bool:
    labels = core.get("initial_labels") or core.get("tags") or []
    return _SOURCE_LABEL in {str(label).strip().lower() for label in labels}


def load_enrollments(home: Path) -> list[dict[str, Any]]:
    path = Path(
        os.environ.get("SKFLEET_REPOSITORIES", str(home / "fleet/repositories.json"))
    ).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceWorktreeError(f"repository enrollment is unreadable: {path}") from exc
    rows = payload.get("repositories") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceWorktreeError("repository enrollment must contain a repositories list")
    return rows


def source_spec(
    core: dict[str, Any], home: Path, enrollments: list[dict[str, Any]] | None = None
) -> SourceSpec | None:
    """Resolve one source card declaration to one clean enrolled checkout."""
    if not is_source_card(core):
        return None
    source = (core.get("meta") or {}).get("source")
    if not isinstance(source, dict):
        raise SourceWorktreeError("source card is missing meta.source")
    repository = source.get("repository")
    base_ref = source.get("base_ref")
    if not isinstance(repository, str) or not _SAFE_NAME.fullmatch(repository):
        raise SourceWorktreeError("source card repository is missing or invalid")
    if not isinstance(base_ref, str) or not base_ref.strip():
        raise SourceWorktreeError("source card base_ref is missing")

    matches = [
        row
        for row in (enrollments if enrollments is not None else load_enrollments(home))
        if isinstance(row, dict) and row.get("name") == repository
    ]
    if len(matches) != 1:
        raise SourceWorktreeError("repository enrollment is absent or ambiguous: %s" % repository)
    row = matches[0]
    checkout_value, remote = row.get("checkout"), row.get("remote")
    if not isinstance(checkout_value, str) or not Path(checkout_value).is_absolute():
        raise SourceWorktreeError("enrolled checkout must be absolute")
    if not isinstance(remote, str) or not remote:
        raise SourceWorktreeError("enrolled repository remote is missing")
    checkout = Path(checkout_value)
    if not checkout.is_dir():
        raise SourceWorktreeError("enrolled checkout is unreadable")
    if _git(checkout, "rev-parse", "--show-toplevel") != str(checkout.resolve()):
        raise SourceWorktreeError("enrolled checkout path is not the git root")
    if _git(checkout, "remote", "get-url", "origin") != remote:
        raise SourceWorktreeError("enrolled checkout origin does not match enrollment")
    if _git(checkout, "status", "--porcelain"):
        raise SourceWorktreeError("enrolled checkout is dirty")
    _git(checkout, "rev-parse", "--verify", "%s^{commit}" % base_ref)
    return SourceSpec(repository, base_ref, checkout.resolve(), remote)


def prepare_worktree(
    home: Path,
    workspace: Path,
    card_id: str,
    core: dict[str, Any],
    agent: str,
    branch: str | None = None,
    enrollments: list[dict[str, Any]] | None = None,
) -> Path:
    """Create and register the clean card worktree, then return its path."""
    spec = source_spec(core, home, enrollments)
    if spec is None:
        return workspace
    if not workspace.is_absolute():
        raise SourceWorktreeError("fleet workspace must be absolute")
    workspace.mkdir(parents=True, exist_ok=True)
    workspace = workspace.resolve()
    target = workspace / ("%s-%s" % (spec.repository, card_id))
    if target.exists() or target.is_symlink():
        raise SourceWorktreeError("card worktree path already exists")
    branch = branch or "feat/%s-source-worktree" % card_id
    base = _git(spec.checkout, "rev-parse", "%s^{commit}" % spec.base_ref)
    _git(spec.checkout, "worktree", "add", "-b", branch, str(target), base)
    try:
        resolved = target.resolve(strict=True)
        if resolved.parent != workspace:
            raise SourceWorktreeError("card worktree escaped fleet workspace")
        if _git(resolved, "rev-parse", "--show-toplevel") != str(resolved):
            raise SourceWorktreeError("created worktree is not its git root")
        if _git(resolved, "status", "--porcelain"):
            raise SourceWorktreeError("created worktree is dirty")
        head = _git(resolved, "rev-parse", "HEAD")
        event_dir = home / "cards" / card_id / "events"
        for event_file in event_dir.glob("*.jsonl"):
            for line in event_file.read_text(encoding="utf-8").splitlines():
                if line.strip() and not isinstance(json.loads(line), dict):
                    raise SourceWorktreeError("CardStore event is not an object")
        CardStore(home).append_event(
            card_id,
            "source_worktree",
            agent,
            repository=spec.repository,
            base_ref=spec.base_ref,
            base=base,
            head=head,
            branch=branch,
            worktree=str(resolved),
            clean=True,
        )
        return resolved
    except Exception:
        # Preservation is deliberate. A partially created worktree is evidence and
        # may contain bytes a later operator needs. Never remove it automatically.
        raise


def _registered_worktree(home: Path, card_id: str) -> dict[str, Any]:
    rows = CardStore(home)._read_events(card_id)
    registrations = [row for row in rows if row.get("action") == "source_worktree"]
    if len(registrations) != 1:
        raise SourceWorktreeError("source card requires exactly one registered worktree")
    return registrations[0]


def validate_source_completion(home: Path, card_id: str, core: dict[str, Any]) -> None:
    """Require clean registered bytes and remote durability for changed source."""
    if not is_source_card(core):
        return
    row = _registered_worktree(home, card_id)
    path = Path(str(row.get("worktree") or ""))
    fleet_root = (home / "fleet/workspaces").resolve()
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SourceWorktreeError("registered worktree is unreadable") from exc
    if fleet_root not in resolved.parents:
        raise SourceWorktreeError("registered worktree is outside fleet workspace")
    if _git(resolved, "rev-parse", "--show-toplevel") != str(resolved):
        raise SourceWorktreeError("registered worktree is not its git root")
    if _git(resolved, "status", "--porcelain"):
        raise SourceWorktreeError("registered worktree is dirty")
    pointed = False
    events = home / "coordination/card_events"
    for event_file in events.glob("*.jsonl"):
        try:
            lines = event_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (
                isinstance(event, dict)
                and event.get("card_id") == card_id
                and str(event.get("link_key") or "").lower() == "worktree"
                and event.get("link_value") == str(resolved)
            ):
                pointed = True
    if not pointed:
        raise SourceWorktreeError("verdict evidence does not point to registered worktree")
    head = _git(resolved, "rev-parse", "HEAD")
    initial = str(row.get("head") or "")
    if head == initial:
        return
    branch = str(row.get("branch") or "")
    remote_head = _git(resolved, "ls-remote", "--heads", "origin", branch)
    fields = remote_head.split()
    if len(fields) != 2 or fields[0] != head:
        raise SourceWorktreeError("source branch or commit is not remotely durable")


def abandoned_worktrees(home: Path) -> list[dict[str, str]]:
    """Inventory registered worktrees without cleaning or deleting any path."""
    findings: list[dict[str, str]] = []
    cards = home / "cards"
    for card_dir in sorted(cards.glob("*")):
        if not (card_dir / "core.json").is_file():
            continue
        for row in CardStore(home)._read_events(card_dir.name):
            if row.get("action") != "source_worktree":
                continue
            path = Path(str(row.get("worktree") or ""))
            if path.exists():
                findings.append(
                    {
                        "card_id": card_dir.name,
                        "worktree": str(path),
                        "branch": str(row.get("branch") or ""),
                        "head": str(row.get("head") or ""),
                    }
                )
    return findings


def main() -> int:
    """Print the preservation inventory as machine-readable JSON."""
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    args = parser.parse_args()
    print(json.dumps({"worktrees": abandoned_worktrees(args.home)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
