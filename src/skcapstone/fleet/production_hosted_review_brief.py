"""Role-specific hosted review instructions using the native guarded link contract."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ..review_admission import reviewer_candidate_reasons
from ..review_verdict import _REQUIRED_CI_LINK_KEYS, _SKCAPSTONE_REPOSITORY, is_review_card
from ..seraph_review_cardstore import LiveCardStoreGateway
from .production_brief import _identity
from .source_bundle import _binding


def production_hosted_review_brief(
    *,
    card_id: str,
    owner: str,
    claim_revision: str,
    workspace: str,
    source_head: str,
    core: dict,
    labels: Sequence[str],
) -> str:
    """Render only the assigned repository's CI and terminal review handoff."""
    _identity(card_id, owner, claim_revision)
    label_set = {str(label).lower() for label in labels}
    bindings = {
        key: _binding(core, key) or ""
        for key in (
            "repository",
            "link_source_card",
            "link_head_revision",
            "producer_identity",
            "candidate_evidence_sha256",
        )
    }
    tree = _binding(core, "candidate_tree")
    if tree:
        bindings["candidate_tree"] = tree
    repository = bindings["repository"]
    if (
        core.get("id") != card_id
        or not is_review_card(core.get("title"))
        or "review" not in label_set
        or "source-only" in label_set
        or not workspace.startswith("/")
        or any(ord(char) < 32 for char in workspace)
        or not repository
        or (core.get("meta") or {}).get("repository") != repository
        or not re.fullmatch(r"[0-9a-f]{8}", bindings["link_source_card"])
        or bindings["link_source_card"] == card_id
        or not re.fullmatch(r"[0-9a-f]{40}", source_head)
        or bindings["link_head_revision"] != source_head
        or (tree is not None and not re.fullmatch(r"[0-9a-f]{40}", tree))
        or not re.fullmatch(r"[0-9a-f]{64}", bindings["candidate_evidence_sha256"])
        or not bindings["producer_identity"]
        or reviewer_candidate_reasons(owner, producer=bindings["producer_identity"])
    ):
        raise ValueError("invalid hosted review source, repository or reviewer binding")
    if tree is None:
        try:
            tree = subprocess.check_output(
                ["git", "-C", workspace, "rev-parse", source_head + "^{tree}"], text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ValueError("hosted review source tree unavailable") from exc
        if not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise ValueError("invalid hosted review source tree")
    skcapstone = repository.rstrip("/").removesuffix(".git") == _SKCAPSTONE_REPOSITORY
    checks = (
        dict.fromkeys(sorted(_REQUIRED_CI_LINK_KEYS), "SUCCESS")
        if skcapstone
        else {"hosted_checks": f"N/N SUCCESS at exact head {source_head}"}
    )
    expected = dict(
        card=card_id,
        owner=owner,
        claim=claim_revision,
        workspace=workspace,
        source_tree=tree,
        bindings=bindings,
        checks=list(checks),
    )
    criteria = "\n".join(str(value) for value in core.get("acceptance_criteria") or [])
    return f"""PRODUCTION HOSTED INDEPENDENT REVIEW
CARD {card_id}; OWNER {owner}; CLAIM {claim_revision}
WORKSPACE {workspace}
REPOSITORY {repository}
SOURCE {bindings['link_source_card']}; HEAD {source_head}; TREE {tree}
PRODUCER {bindings['producer_identity']}; CANDIDATE SHA256 {bindings['candidate_evidence_sha256']}

You are the reviewer. Read workspace AGENTS.md and the exact card TDD below.
Verify the folded owner, claim, dependencies and exact source before review.
Preserve HEAD and a clean workspace. No source changes, evidence commits, push,
merge, deployment, service changes or external actions. Use only authorized
repository/evidence roots and bounded rg reads. All board writes use skcapstone
coord; never edit CardStore. Retain workspace, evidence and claim custody.
Do not self-complete, release your claim, take another card or send unsolicited mail.
Honor privacy, egress and Matter boundaries and the qualified independent gateway
route. Record actual served provider attribution; argv alone is not proof.
Run ls after every file write. After any authorized commit run git rev-parse HEAD
and echo the hash. Stop immediately on inconsistent output. At the second
auto-compaction write a new private .handoff.md (preserve any existing handoff),
finish the current step and stop for a fresh session.

Independently verify candidate bytes/hash, diff and required tests. Record actual
commands, findings, failures, limitations and inspected versus executed checks.
Terminal decision: PASS only with no blocking findings and observed required
exact-head CI; otherwise BLOCKED blocked_on=<dependency|card|human|capability>
referent=<exact requirement> <actual reason>. Never invent SUCCESS or approval.
Only this repository's hosted CI fields apply:
{json.dumps(checks, sort_keys=True)}
For PASS set REVIEW_CHECKS_JSON to those observed values (replace N with the
actual positive passed/total count if shown). Bind the original head above.
For BLOCKED no CI SUCCESS fields are required or published.

Write one canonical summary under ~/.skcapstone/evidence/work/{card_id}/ in an
owned 0700 directory with owned regular 0600 files, no symlinks. Include provider
receipt paths and their computed hashes inside that summary. All native evidence
digest aliases refer to this SAME summary, never a different raw receipt.
Set REVIEW_EVIDENCE to its plain absolute path, with no #sha256= or |sha256= suffix;
set REVIEW_VERDICT to the actual terminal decision. The recipe computes hashes
from bytes, checks source/claim and publishes guarded links. It makes no commit.

```bash
set -euo pipefail
export REVIEW_EVIDENCE REVIEW_VERDICT REVIEW_CHECKS_JSON
python3 -m skcapstone.fleet.production_hosted_review_brief --handoff \\
    {shlex.quote(json.dumps(expected, sort_keys=True))}
```

Stop after handoff or any rejection; there is no unguarded fallback. Controller
verification and native completion gates remain required. Evidence links precede
the verdict here; no blanket rule claims that every later link invalidates it.

TITLE: {core.get('title', '')}
TASK TDD:
{core.get('description', '')}
ACCEPTANCE CRITERIA:
{criteria}
"""


def _hosted_review_handoff(expected: dict) -> None:
    """Publish the brief-bound private summary through guarded native links."""
    e = expected
    card = e["card"]
    home = Path.home() / ".skcapstone"

    def git(*args):
        """Read the unchanged candidate identity and workspace state."""
        return subprocess.check_output(["git", *args], text=True).strip()

    if (
        str(Path.cwd()) != e["workspace"]
        or git("rev-parse", "--show-toplevel") != e["workspace"]
        or git("status", "--porcelain")
        or git("rev-parse", "HEAD") != e["bindings"]["link_head_revision"]
        or git("rev-parse", "HEAD^{tree}") != e["source_tree"]
    ):
        raise ValueError("review source changed; preserve workspace and stop")
    revision = LiveCardStoreGateway(home).read_card(card).revision
    c = json.loads(
        subprocess.check_output(["skcapstone", "coord", "show", card, "--json"], text=True)
    )
    if (
        c.get("id") != card
        or c.get("owner") != e["owner"]
        or c.get("meta", {}).get("_claim_revision") != e["claim"]
        or c.get("archived")
        or c.get("status") not in ("ready", "doing", "review")
        or c.get("meta", {}).get("claim_conflicts")
        or "review" not in c.get("labels", [])
        or "source-only" in c.get("labels", [])
        or any(_binding(c, key) != value for key, value in e["bindings"].items())
    ):
        raise ValueError("review ownership or source binding changed")
    verdict = os.environ["REVIEW_VERDICT"]
    if verdict != "PASS" and not re.fullmatch(
        r"BLOCKED blocked_on=(dependency|card|human|capability) referent=\S+ .+", verdict
    ):
        raise ValueError("use terminal PASS or precise BLOCKED with category, referent and reason")
    checks = {}
    if verdict == "PASS":
        checks = json.loads(os.environ["REVIEW_CHECKS_JSON"])
        if not isinstance(checks, dict) or set(checks) != set(e["checks"]):
            raise ValueError("missing or unrelated repository checks")
        for key, value in checks.items():
            if key.startswith("ci_check_"):
                valid = value == "SUCCESS"
            else:
                match = re.fullmatch(
                    r"([1-9][0-9]*)/([1-9][0-9]*) SUCCESS at exact head ([0-9a-f]{40})", value
                )
                valid = (
                    match
                    and match[1] == match[2]
                    and match[3] == e["bindings"]["link_head_revision"]
                )
            if not valid:
                raise ValueError("missing, incomplete or stale hosted check")
    path = Path(os.environ["REVIEW_EVIDENCE"])
    root = home / "evidence/work" / card
    relative = path.relative_to(root)
    if not path.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("evidence must be a plain path beneath the private card directory")
    fd = os.open(Path.home(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in (".skcapstone", "evidence", "work", card, *relative.parts[:-1]):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or (
                part == card and stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise ValueError("evidence directory must be owned; card directory mode 0700")
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not info.st_size
            ):
                raise ValueError("evidence must be a nonempty owned regular file with mode 0600")
            digest = hashlib.sha256(stream.read()).hexdigest()
    finally:
        os.close(fd)
    links = dict(
        evidence=str(path),
        review_evidence=str(path),
        review_evidence_sha256=digest,
        evidence_sha256=digest,
        reviewer_evidence_sha256=digest,
    )
    for key, value in links.items():
        if c.get("meta", {}).get(key) not in (None, value):
            raise ValueError("immutable evidence binding conflicts; stop for controller")
    for key in ("review_evidence_sha256", "evidence_sha256", "reviewer_evidence_sha256"):
        if c.get("links", {}).get(key) not in (None, digest):
            raise ValueError("existing evidence digest differs; stop for controller")
    links.update(checks)
    links["verdict"] = verdict
    for key, value in links.items():
        transition = hashlib.sha256(
            json.dumps([card, e["claim"], revision, key, value], separators=(",", ":")).encode()
        ).hexdigest()
        reply = json.loads(
            subprocess.check_output(
                [
                    "skcapstone",
                    "coord",
                    "link",
                    card,
                    key,
                    value,
                    "--agent",
                    e["owner"],
                    "--expected-source-revision",
                    revision,
                    "--expected-claim-revision",
                    e["claim"],
                    "--transition-id",
                    transition,
                    "--json",
                ],
                text=True,
            )
        )
        if reply.get("card_id") != card or not re.fullmatch(
            "[0-9a-f]{64}", reply.get("source_revision", "")
        ):
            raise ValueError("guarded link readback invalid; stop without fallback")
        revision = reply["source_revision"]
    print(json.dumps(dict(evidence=str(path), evidence_sha256=digest, verdict=verdict)))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--handoff":
        raise SystemExit(
            "usage: python -m skcapstone.fleet.production_hosted_review_brief --handoff JSON"
        )
    _hosted_review_handoff(json.loads(sys.argv[2]))
