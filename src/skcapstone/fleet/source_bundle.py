"""Native production transfer of exact unpublished source candidates."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

MAX_BUNDLE = 8 * 1024 * 1024
MAX_EVIDENCE = 256 * 1024


class SourceBundleError(ValueError):
    """Exact source artifacts are unavailable, changed or unsafe."""


def _read(path: Path, limit: int) -> bytes:
    """Read a bounded private regular file without following redirected paths."""
    if path.resolve() != path.absolute():
        raise SourceBundleError("source artifact path is redirected")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise SourceBundleError("source artifact file is unsafe")
        raw = stream.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise SourceBundleError("source artifact exceeds bound")
    return raw


def _sha(raw: bytes) -> str:
    """Return the immutable content address."""
    return hashlib.sha256(raw).hexdigest()


def _root(home: Path, card: str) -> Path:
    """Restrict shared source artifacts to the exact native producer card."""
    if not re.fullmatch(r"[0-9a-f]{8}", card):
        raise SourceBundleError("source artifact card identity invalid")
    return home / "evidence" / "work" / card / "source-bundles"


def _once(path: Path, raw: bytes) -> None:
    """Publish complete immutable bytes by exclusive link, preserving retries."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.resolve() != path.parent or path.parent.stat().st_mode & 0o022:
        raise SourceBundleError("source artifact directory is unsafe")
    fd, temporary = tempfile.mkstemp(prefix=".source-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read(path, max(MAX_BUNDLE, len(raw))) != raw:
                raise SourceBundleError("source artifact immutable collision") from None
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary)


# This is operator-owned inspection code. Worker Git configuration can only
# run inside a read-only namespace without host home, secrets or networking.
_EXPORT = r"""
import base64, hashlib, json, os, resource, stat, subprocess, sys
from pathlib import Path
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
card, base, head, tree, ref = sys.argv[1:]
root=Path('/work')
assert (root/'.git').is_dir() and not (root/'.git').is_symlink()
assert not os.path.lexists(root/'.git/objects/info/alternates')
assert not os.path.lexists(root/'.git/objects/info/http-alternates')
def git(*args):
    return subprocess.run(['/usr/bin/git','--no-replace-objects','--git-dir=/work/.git',
        '--work-tree=/work','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null',*args],
        check=True,capture_output=True,timeout=20).stdout
assert git('rev-parse','HEAD^{commit}').decode().strip()==head
assert git('rev-parse','HEAD^{tree}').decode().strip()==tree
assert git('symbolic-ref','HEAD').decode().strip()==ref
assert head!=base
git('merge-base','--is-ancestor',base,head)
assert not git('status','--porcelain','--untracked-files=all')
relative='docs/evidence/agents/'+card+'/COMPLETION-EVIDENCE.md'
path=root
for part in relative.split('/'):
    path=path/part
    assert not path.is_symlink()
with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as f:
    assert stat.S_ISREG(os.fstat(f.fileno()).st_mode)
    evidence=f.read(262145)
assert 0<len(evidence)<=262144
assert git('cat-file','blob',head+':'+relative)==evidence
bundle=git('bundle','create','-',ref,'^'+base)
assert 0<len(bundle)<=8*1024*1024
assert git('rev-parse','HEAD^{commit}').decode().strip()==head
print(json.dumps({'bundle_b64':base64.b64encode(bundle).decode(),
                 'evidence_sha256':hashlib.sha256(evidence).hexdigest()}))
"""


def _export(workspace: Path, card: str, base: str, outcome: dict) -> dict:
    """Run bounded export without executing repository configuration on host."""
    if workspace.resolve() != workspace or not workspace.is_dir():
        raise SourceBundleError("source workspace is redirected")
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    command += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        str(workspace),
        "/work",
        "--chdir",
        "/work",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "GIT_CONFIG_NOSYSTEM",
        "1",
        "--setenv",
        "GIT_OPTIONAL_LOCKS",
        "0",
        "/usr/bin/python3",
        "-I",
        "-c",
        _EXPORT,
        card,
        base,
        outcome["candidate_commit"],
        outcome["candidate_tree"],
        outcome["candidate_ref"],
    ]
    try:
        result = subprocess.run(command, capture_output=True, timeout=45)
        if result.returncode or len(result.stdout) > 2 * MAX_BUNDLE:
            raise SourceBundleError("source candidate export refused")
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SourceBundleError("source candidate export refused") from exc


def publish_source(
    home: Path,
    request: dict,
    owner: str,
    claim: str,
    workspace: Path,
    *,
    acknowledged: str | None = None,
) -> dict:
    """Retain an exact current producer proposal, without approving completion."""
    from skcoord.card_store import CardStore, card_mutation_lock

    from ..seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome

    card_id = request["card_id"]
    with card_mutation_lock(home, card_id):
        store = CardStore(home)
        row = store.fold(card_id)
        if (
            row is None
            or "source-only" not in row.labels
            or row.owner != owner
            or row.meta.get("_claim_revision") != claim
        ):
            raise SourceBundleError("source proposal claim changed")
        for key in ("repository", "base_revision"):
            if _binding({"meta": row.meta, "links": row.links}, key) != request.get(key):
                raise SourceBundleError("source proposal repository or base changed")
        snapshot = LiveCardStoreGateway(home).read_card(card_id)
        outcome = _latest_outcome(store, card_id)
        if (
            snapshot.verdict != "PASS_FOR_REVIEW"
            or outcome.get("action") != "verdict"
            or outcome.get("writer") != owner
            or snapshot.producer_identity != owner
        ):
            raise SourceBundleError("source proposal lacks a current typed review request")
        if outcome.get("expected_claim_revision") not in (None, claim):
            raise SourceBundleError("source proposal belongs to another claim")
        events = store._read_events(card_id)
        claims = [
            event
            for event in events
            if event.get("action") == "claim"
            and (event.get("claim_revision") or event.get("event_id")) == claim
        ]
        if len(claims) != 1 or str(outcome.get("ts", "")) < str(claims[0].get("ts", "")):
            raise SourceBundleError("source proposal predates its claim")
        for key in ("candidate_commit", "candidate_tree"):
            if not re.fullmatch(r"[0-9a-f]{40}", str(outcome.get(key, ""))):
                raise SourceBundleError("source proposal git binding invalid")
        if not re.fullmatch(
            r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", str(outcome.get("candidate_ref", ""))
        ):
            raise SourceBundleError("source proposal reference invalid")
        root = _root(home, card_id)
        evidence = Path(str(outcome.get("candidate_path", "")))
        if root.parent not in evidence.parents:
            raise SourceBundleError("source proposal evidence is not shared under its card")
        if _sha(_read(evidence, MAX_EVIDENCE)) != outcome.get("candidate_sha256"):
            raise SourceBundleError("source proposal evidence changed")
        path = root / (outcome["candidate_commit"] + ".json")
        if path.exists():
            # Retained, machine-qualified bytes are the candidate. Reconciliation
            # does not re-export or re-upload the same immutable proposal.
            manifest = json.loads(_read(path, MAX_EVIDENCE))
            if not isinstance(manifest, dict):
                raise SourceBundleError("retained candidate manifest is not an object")
            expected = {
                "schema": "skfleet.source-bundle/v1",
                "card": card_id,
                "owner": owner,
                "claim_revision": claim,
                "repository": request["repository"],
                "base_revision": request["base_revision"],
                "head": outcome["candidate_commit"],
                "tree": outcome["candidate_tree"],
                "ref": outcome["candidate_ref"],
                "evidence_sha256": outcome["candidate_sha256"],
            }
            if any(manifest.get(key) != value for key, value in expected.items()):
                raise SourceBundleError("retained candidate belongs to another generation")
            from .source_transport import _packet, push

            packet = _packet(home, card_id, manifest["head"])
            raw = base64.b64decode(packet["bundle"], validate=True)
            if _sha(raw) != manifest["bundle_sha256"] or len(raw) != manifest["bundle_bytes"]:
                raise SourceBundleError("retained candidate bundle changed")
            manifest_sha = _sha(_read(path, MAX_EVIDENCE))
            if acknowledged != manifest_sha:
                push(home, manifest)
            return {"manifest": str(path), "manifest_sha256": manifest_sha, **manifest}
        packet = _export(workspace, card_id, request["base_revision"], outcome)
        raw = base64.b64decode(packet["bundle_b64"], validate=True)
        if (
            not 0 < len(raw) <= MAX_BUNDLE
            or packet["evidence_sha256"] != outcome["candidate_sha256"]
        ):
            raise SourceBundleError("source candidate differs from typed proposal")
        if LiveCardStoreGateway(home).read_card(card_id).revision != snapshot.revision:
            raise SourceBundleError("source proposal changed during export")
        manifest = {
            "schema": "skfleet.source-bundle/v1",
            "card": card_id,
            "owner": owner,
            "claim_revision": claim,
            "repository": request["repository"],
            "base_revision": request["base_revision"],
            "head": outcome["candidate_commit"],
            "tree": outcome["candidate_tree"],
            "ref": outcome["candidate_ref"],
            "evidence_sha256": outcome["candidate_sha256"],
            "bundle_sha256": _sha(raw),
            "bundle_bytes": len(raw),
        }
        _once(root / (manifest["bundle_sha256"] + ".bundle"), raw)
        _once(root / (manifest["evidence_sha256"] + ".md"), _read(evidence, MAX_EVIDENCE))
        path = root / (manifest["head"] + ".json")
        _once(path, json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
        from .source_transport import push

        push(home, manifest)
        return {
            "manifest": str(path),
            "manifest_sha256": _sha(_read(path, MAX_EVIDENCE)),
            **manifest,
        }


def _binding(core: dict, key: str) -> str | None:
    """Reject conflicting native review metadata and link projections."""
    if any(not isinstance(core.get(container) or {}, dict) for container in ("meta", "links")):
        raise SourceBundleError("review source metadata malformed")
    values = {
        str((core.get(container) or {}).get(key)).strip()
        for container in ("meta", "links")
        if (core.get(container) or {}).get(key) is not None
    }
    if len(values) > 1:
        raise SourceBundleError("review source binding conflict")
    return next(iter(values), None)


def _review_manifest(core: dict, repository: str, head: str) -> tuple[dict, Path] | None:
    """Bind shared artifact bytes to the exact governed native review source."""
    card = _binding(core, "link_source_card")
    if card is None:
        return None
    home = Path(os.environ.get("SKCAPSTONE_HOME", str(Path.home() / ".skcapstone"))).expanduser()
    root = _root(home, card)
    if not re.fullmatch(r"[0-9a-f]{40}", head) or _binding(core, "link_head_revision") != head:
        raise SourceBundleError("review source head binding invalid")
    path = root / (head + ".json")
    producer = _binding(core, "producer_identity") or ""
    if not path.exists() and re.match(r"pi-(codex|glm|deepseek|qwen)-builder-", producer):
        from .source_transport import pull

        pull(home, card, head)
    if not path.exists() and not re.match(r"pi-(codex|glm|deepseek|qwen)-builder-", producer):
        return None
    try:
        manifest = json.loads(_read(path, MAX_EVIDENCE))
        if not isinstance(manifest, dict):
            raise SourceBundleError("review source manifest is not an object")
        if (
            manifest.get("schema") != "skfleet.source-bundle/v1"
            or manifest.get("card") != card
            or manifest.get("head") != head
            or manifest.get("repository") != repository
            or manifest.get("owner") != producer
            or manifest.get("evidence_sha256") != _binding(core, "candidate_evidence_sha256")
        ):
            raise SourceBundleError("review source manifest binding mismatch")
        for key, width in (("base_revision", 40), ("tree", 40), ("bundle_sha256", 64)):
            if not re.fullmatch(r"[0-9a-f]{" + str(width) + "}", str(manifest.get(key, ""))):
                raise SourceBundleError("review source manifest hash invalid")
        if not re.fullmatch(
            r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", str(manifest.get("ref", ""))
        ):
            raise SourceBundleError("review source manifest reference invalid")
        for key, value in (
            ("candidate_tree", manifest["tree"]),
            ("candidate_ref", manifest["ref"]),
        ):
            if _binding(core, key) not in (None, value):
                raise SourceBundleError("review source candidate binding mismatch")
        blob = root / (manifest["bundle_sha256"] + ".bundle")
        raw = _read(blob, MAX_BUNDLE)
        if len(raw) != manifest.get("bundle_bytes") or _sha(raw) != manifest["bundle_sha256"]:
            raise SourceBundleError("review source bundle digest mismatch")
        return manifest, blob
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise SourceBundleError("review source artifacts unavailable or invalid") from exc


def verify_review_source(core: dict, repository: str, base_revision: str) -> bool:
    """Qualify exact native artifact binding before bypassing remote ref lookup."""
    return _review_manifest(core, repository, base_revision) is not None


def import_review_source(core: dict, repository: str, base_revision: str, workspace: Path) -> bool:
    """Import into a new private checkout; never reset existing source custody."""
    selected = _review_manifest(core, repository, base_revision)
    if selected is None:
        return False
    manifest, blob = selected
    workspace = Path(workspace)
    if os.path.lexists(workspace):
        raise SourceBundleError("review source workspace already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".review-source-", dir=workspace.parent))

    def git(*args):
        result = subprocess.run(
            [
                "/usr/bin/git",
                "--no-replace-objects",
                "-C",
                str(temporary),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "fetch.fsckObjects=true",
                "-c",
                "transfer.fsckObjects=true",
                *args,
            ],
            capture_output=True,
            timeout=45,
        )
        if result.returncode:
            raise SourceBundleError("review source object import failed")
        return result.stdout.decode().strip()

    try:
        git("init", "--quiet")
        git("remote", "add", "origin", repository)
        git("fetch", "--quiet", "--depth=1", "origin", manifest["base_revision"])
        git("bundle", "verify", str(blob))
        if git("bundle", "list-heads", str(blob)).split() != [base_revision, manifest["ref"]]:
            raise SourceBundleError("review bundle advertised head differs")
        git(
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--no-tags",
            str(blob),
            manifest["ref"],
        )
        if git("rev-parse", base_revision + "^{tree}") != manifest["tree"]:
            raise SourceBundleError("review source tree differs")
        git("merge-base", "--is-ancestor", manifest["base_revision"], base_revision)
        git("checkout", "--quiet", "--detach", base_revision)
        _review_manifest(core, repository, base_revision)
        if os.path.lexists(workspace):
            raise SourceBundleError("review source workspace appeared during import")
        temporary.rename(workspace)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceBundleError("review source import refused") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
