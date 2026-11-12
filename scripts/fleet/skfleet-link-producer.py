#!/home/skuser01/.skenv/bin/python
"""Refresh Link lineage and publish its mediated observation feed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from skcapstone.forgejo import SKGIT_REPOSITORY
from skcapstone.link_fetch_share import fresh_run_env

REPOSITORY_SCOPE_ENV = "SKFLEET_LINK_REPOSITORIES"

#: The scope this package ships, used when the estate declares none of its own.
#: Keeping it means an estate that has not written a scope file behaves exactly
#: as before rather than failing closed on upgrade.
REQUIRED_REPOSITORIES = frozenset(
    {
        "smilinTux/skcapstone",
        "smilinTux/skdashboard",
        "smilinTux/skworld",
        "smilinTux/sk-standards",
    }
)

#: Where an estate declares its own lifecycle repository scope, relative to the
#: SKCapstone home. The packaged set above is correct for nor and wrong for any
#: estate watching different repositories, and it was the only answer the
#: producer would accept: a scope that did not equal it exactly exited 78
#: `repository_scope_invalid`, so a second estate could not be configured at
#: all without editing this file. Reading it from the estate's own coordination
#: tree makes the producer estate agnostic without weakening the check below.
REPOSITORY_SCOPE_FILE = Path("coordination") / "link-scope.json"
REPOSITORY_SCOPE_SCHEMA = "skfleet.link-scope/v1"

#: Where an estate keeps its bounded exclusion records, relative to the
#: SKCapstone home. `link-lineage.py` has accepted `--exclude FILE` all along
#: and validates each record hard (head and base SHA bound to the observed PR,
#: owner, reason, classification, evidence hash, expiry), but the producer
#: never passed the flag, so the whole mechanism was unreachable from the one
#: path that actually runs the reconciler.
EXCLUSIONS_FILE = Path("coordination") / "link-lineage-exclusions.json"


def declared_repositories(home: Path | None = None) -> frozenset[str]:
    """The lifecycle repository set this estate declares.

    Falls back to :data:`REQUIRED_REPOSITORIES` when the estate declares
    nothing, which is what keeps an un-migrated node working. A scope file that
    is present but malformed is NOT silently ignored: a scope the operator
    believes is in force but which the producer quietly replaced with the
    packaged default is worse than a hard failure, so it raises.
    """
    if home is None:
        return REQUIRED_REPOSITORIES
    path = home / REPOSITORY_SCOPE_FILE
    if not path.is_file():
        return REQUIRED_REPOSITORIES
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != REPOSITORY_SCOPE_SCHEMA:
        raise ValueError(f"{path} is not a {REPOSITORY_SCOPE_SCHEMA} document")
    declared = document.get("repositories")
    if not isinstance(declared, list) or not declared:
        raise ValueError(f"{path} declares no repositories")
    if any(not isinstance(entry, str) or not entry.strip() for entry in declared):
        raise ValueError(f"{path} declares a malformed repository")
    stripped = [entry.strip() for entry in declared]
    if len(stripped) != len(set(stripped)):
        raise ValueError(f"{path} declares a duplicate repository")
    return frozenset(stripped)


def repository_scope(home: Path | None = None) -> tuple[str, ...] | None:
    """Return the exact configured lifecycle repository scope.

    The equality check against the declared set is deliberate and stays. The
    unit's environment is what the producer actually fetches, and a narrower
    environment would produce a feed that looks complete while ignoring whole
    repositories. So the environment has to match the declaration exactly, and
    all this change does is move the declaration out of this file and into the
    estate's own.
    """
    raw = os.environ.get(REPOSITORY_SCOPE_ENV, "")
    parts = raw.split(",")
    repositories = tuple(part.strip() for part in parts)
    declared = declared_repositories(home)
    if (
        not repositories
        or any(not repository for repository in repositories)
        or len(repositories) != len(set(repositories))
        or set(repositories)
        not in (
            declared,
            declared | {SKGIT_REPOSITORY},
        )
    ):
        return None
    return repositories


def run(
    *,
    home: Path,
) -> int:
    """Run the pipeline with one forge fetch shared by both stages.

    The cache lives in a private directory removed when the run ends, under a run
    id that is new every run, so no later run can be served this run's PR data.
    """
    scratch = Path(tempfile.mkdtemp(prefix="skfleet-link-run-"))
    try:
        return _run(home=home, env={**os.environ, **fresh_run_env(scratch, uuid.uuid4().hex)})
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run(*, home: Path, env: dict[str, str]) -> int:
    """Run the role-bounded producer pipeline and preserve incomplete fallback."""
    if (
        os.environ.get("SKAGENT") != "link-producer"
        or os.environ.get("SKCAPSTONE_AGENT") != "link-producer"
    ):
        print('{"healthy":false,"reason":"producer_identity_required"}')
        return 77
    try:
        repositories = repository_scope(home)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(json.dumps({"healthy": False, "reason": "repository_scope_unreadable",
                          "detail": str(error)}))
        return 78
    if repositories is None:
        print('{"healthy":false,"reason":"repository_scope_invalid"}')
        return 78

    coordination = home / "coordination"
    lineage = coordination / "link-lineage.json"
    output = coordination / "link-observations.json"
    reconciler = Path(__file__).with_name("link-lineage.py")
    # The exclusion file is passed when the estate has one and omitted when it
    # does not, so an estate that excludes nothing behaves exactly as before.
    # Its CONTENTS stay an operator decision: the reconciliation doc forbids
    # excluding a PR by inference from title, age, author or repository history,
    # and link-lineage.py rejects any record that is not bound to the PR's
    # current head and base SHA. This only makes the mechanism reachable.
    exclusions = home / EXCLUSIONS_FILE
    exclude_arguments = ["--exclude", str(exclusions)] if exclusions.is_file() else []
    reconcile = subprocess.run(
        [
            sys.executable,
            str(reconciler),
            "--home",
            str(home),
            "--output",
            str(lineage),
        ]
        + exclude_arguments
        + [argument for repository in repositories for argument in ("--repo", repository)],
        check=False,
        env=env,
    )
    if reconcile.returncode:
        return reconcile.returncode

    produce = subprocess.run(
        [
            sys.executable,
            "-m",
            "skcapstone.link_observation_producer",
            "--lineage",
            str(lineage),
            "--output",
            str(output),
        ]
        + [argument for repository in repositories for argument in ("--repo", repository)],
        check=False,
        env=env,
        capture_output=True,
        text=True,
    )
    if produce.stdout:
        print(produce.stdout, end="")
    if produce.stderr:
        print(produce.stderr, end="", file=sys.stderr)
    if produce.returncode != 78:
        return produce.returncode
    try:
        result = json.loads(produce.stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return produce.returncode
    return 0 if result.get("reason") == "lineage_incomplete" else produce.returncode


def main() -> int:
    """Run against the current user's SKCapstone home."""
    return run(home=Path.home() / ".skcapstone")


if __name__ == "__main__":
    raise SystemExit(main())
