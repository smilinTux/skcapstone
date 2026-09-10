#!/usr/bin/env python3
"""Detect stale git sequencer state in worktrees; clear it when safe.

Why: an abandoned cherry-pick sequence from one card (clean tree, queued
picks) blocked git operations for the next worker in a shared worktree until
someone ran git cherry-pick --abort by hand. Stale sequencer state must be
visible, named, and clearable when the working tree is clean.

Default mode reports. --clear aborts the named sequence, but only when the
working tree is clean. Dirty-tree stale state is never auto-cleared; it is
reported loudly for a human.

Exit codes: 0 nothing stale or successfully cleared, 1 stale found in report
mode, 2 stale with dirty tree (never cleared), 3 not a git worktree.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

# marker inside the git dir -> abort command that clears it
MARKERS = [
    ("sequencer", ["cherry-pick", "--abort"]),
    ("rebase-merge", ["rebase", "--abort"]),
    ("rebase-apply", ["rebase", "--abort"]),
    ("MERGE_HEAD", ["merge", "--abort"]),
    ("CHERRY_PICK_HEAD", ["cherry-pick", "--abort"]),
    ("REVERT_HEAD", ["revert", "--abort"]),
]


def git_dir(path: str):
    r = subprocess.run(
        ["git", "-C", path, "rev-parse", "--git-dir"], capture_output=True, text=True
    )
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out if os.path.isabs(out) else os.path.normpath(os.path.join(path, out))


def is_clean(path: str) -> bool:
    r = subprocess.run(
        ["git", "-C", path, "status", "--porcelain"], capture_output=True, text=True
    )
    return r.returncode == 0 and not r.stdout.strip()


def detect(path: str) -> dict:
    gd = git_dir(path)
    if gd is None:
        return {"path": path, "state": "NOT_A_REPO", "markers": [], "clean": None, "git_dir": None}
    markers = [name for name, _ in MARKERS if os.path.exists(os.path.join(gd, name))]
    if not markers:
        return {"path": path, "state": "OK", "markers": [], "clean": None, "git_dir": gd}
    clean = is_clean(path)
    return {
        "path": path,
        "state": "CLEAN_TREE" if clean else "DIRTY_TREE",
        "markers": markers,
        "clean": clean,
        "git_dir": gd,
    }


def abort_for(marker: str) -> list:
    for name, cmd in MARKERS:
        if name == marker:
            return cmd
    return ["cherry-pick", "--abort"]


def clear(finding: dict) -> tuple:
    marker = finding["markers"][0]
    r = subprocess.run(
        ["git", "-C", finding["path"], *abort_for(marker)], capture_output=True, text=True
    )
    message = r.stderr.strip() or r.stdout.strip() or "aborted"
    return r.returncode == 0, message


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Clear or name stale sequencer state")
    parser.add_argument(
        "paths",
        nargs="*",
        default=[os.getcwd()],
        help="worktree paths; defaults to the current directory",
    )
    parser.add_argument(
        "--clear", action="store_true", help="abort stale sequences when the working tree is clean"
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for path in args.paths:
        finding = detect(path)
        state, markers = finding["state"], finding["markers"]
        if state == "NOT_A_REPO":
            print(f"ERROR   {path}: not a git worktree")
            exit_code = max(exit_code, 3)
            continue
        if state == "OK":
            print(f"OK      {path}")
            continue
        if state == "DIRTY_TREE":
            print(
                f"BLOCKED {path}: stale {','.join(markers)} with DIRTY working tree; "
                f"resolve by hand, never auto-cleared"
            )
            exit_code = max(exit_code, 2)
            continue
        # CLEAN_TREE stale state
        if args.clear:
            ok, message = clear(finding)
            suffix = "cleared" if ok else f"clear FAILED: {message}"
            print(f"CLEARED {path}: stale {','.join(markers)} ({suffix})")
            if not ok:
                exit_code = max(exit_code, 2)
        else:
            print(
                f"STALE   {path}: {','.join(markers)} with clean tree; "
                f"rerun with --clear to abort"
            )
            exit_code = max(exit_code, 1)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
