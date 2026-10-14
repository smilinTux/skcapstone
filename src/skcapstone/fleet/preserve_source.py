"""Operator preservation of an exact source commit without changing its identity."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..seat_boundaries import CoordActorClass, classify_coord_actor
from ..source_binding import source_binding_meta
from ..source_ref_amendment import prove_ref
from .builder_dispatch import SSH_SOURCE_TRANSPORTS
from .source_bundle import _binding


def _git(*args, cwd=None, check=True):
    """Run bounded Git without credentials, hooks, replacements or global rewrites."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes",
    }
    result = subprocess.run(
        [
            "/usr/bin/git",
            "--no-replace-objects",
            "-c",
            "credential.helper=",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *map(str, args),
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode:
        raise ValueError("source preservation Git operation failed: " + str(args[0]))
    return result


def _coord(home, *args):
    """Use the audited coord boundary for every board read and write."""
    result = subprocess.run(
        ["skcapstone", "coord", *args, "--home", str(home)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("source preservation coord operation refused")
    return json.loads(result.stdout) if args[0] == "show" else result.stdout


def binding(card):
    """Require unchanged repository/revision and an inactive, nonterminal card."""
    if (
        card.get("owner")
        or card.get("archived")
        or card.get("status") not in {"backlog", "ready", "review"}
    ):
        raise ValueError("source preservation requires an unowned open card")
    meta, links = card.get("meta") or {}, card.get("links") or {}
    for key in ("repository", "base_revision"):
        if key in meta and key in links and meta[key] != links[key]:
            raise ValueError("source preservation binding conflict: " + key)
    value = source_binding_meta(
        [], *[links.get(k, meta.get(k)) for k in ("repository", "base_ref", "base_revision")]
    )
    url = urlsplit(value["repository"])
    if (
        url.hostname not in SSH_SOURCE_TRANSPORTS
        or url.port
        or not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url.path)
    ):
        raise ValueError("source preservation requires an unchanged skgit repository binding")
    return value


def default_roots(home, card_id):
    """Search known local workspace registries, never the estate or mounted NFS."""
    return [
        Path.home() / "work",
        Path("/srv/sklegal-fast/work"),
        home / "fleet/workspaces",
        home / "evidence/work" / card_id / "source-bundles",
    ]


def candidates(roots):
    """Bound discovery to three levels of explicit roots; do not follow symlinks."""
    seen = set()
    for root in roots:
        root = Path(root).expanduser()
        if root.is_symlink() or not root.exists():
            continue
        if root.is_file():
            yield root
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            path = Path(directory)
            depth = len(path.relative_to(root).parts)
            if len(seen) >= 4096:
                raise ValueError("source search exceeds bounded candidate inventory")
            if ".git" in dirs or ".git" in files or ("HEAD" in files and "objects" in dirs):
                if path not in seen:
                    seen.add(path)
                    yield path
                dirs[:] = []
                continue
            for name in files:
                item = path / name
                if name.endswith(".bundle") and not item.is_symlink():
                    seen.add(item)
                    yield item
            dirs[:] = [d for d in dirs if not (path / d).is_symlink()] if depth < 3 else []


def find_source(roots, revision):
    """Locate the exact commit, including self-contained Git source bundles."""
    for path in candidates(roots):
        if path.is_file():
            with tempfile.TemporaryDirectory(prefix="skfleet-preserve-inspect-") as tmp:
                _git("init", "--bare", "-q", tmp)
                result = _git(
                    "fetch",
                    "--no-tags",
                    str(path),
                    "+refs/*:refs/candidates/*",
                    cwd=tmp,
                    check=False,
                )
                if (
                    result.returncode == 0
                    and _git("cat-file", "-t", revision, cwd=tmp, check=False).stdout.strip()
                    == "commit"
                ):
                    return path
        elif _git("-C", path, "cat-file", "-t", revision, check=False).stdout.strip() == "commit":
            return path
    raise ValueError("exact base_revision not found in bounded local clones or source bundles")


def publish(source, remote, ref, revision, *, ancestor=None):
    """Create only: an absent-ref lease rejects even a concurrent fast-forward."""
    current = _git("ls-remote", remote, ref).stdout.splitlines()
    if current:
        if current != [revision + "\t" + ref]:
            raise ValueError("immutable preservation ref conflict")
        return
    with tempfile.TemporaryDirectory(prefix="skfleet-preserve-publish-") as tmp:
        _git("init", "--bare", "-q", tmp)
        if source.is_file():
            _git("fetch", "--no-tags", source, "+refs/*:refs/candidates/*", cwd=tmp)
        else:
            _git("fetch", "--no-tags", source, revision, cwd=tmp)
        if _git("cat-file", "-t", revision, cwd=tmp).stdout.strip() != "commit":
            raise ValueError("source preservation requires the exact commit object")
        if (
            ancestor is not None
            and _git(
                "merge-base", "--is-ancestor", ancestor, revision, cwd=tmp, check=False
            ).returncode
        ):
            raise ValueError("producer base is not an ancestor of the exact review candidate")
        # Empty expected value is compare-and-create, NOT permission to update.
        # No '+' refspec or '--force': even an existing ancestor must refuse.
        result = _git(
            "push",
            "--porcelain",
            "--force-with-lease=" + ref + ":",
            remote,
            revision + ":" + ref,
            cwd=tmp,
            check=False,
        )
        if result.returncode:
            if _git("ls-remote", remote, ref).stdout.strip() != revision + "\t" + ref:
                raise ValueError("immutable preservation ref conflict or publication refused")
    if _git("ls-remote", remote, ref).stdout.strip() != revision + "\t" + ref:
        raise ValueError("preservation ref readback differs")


def preserve(home, card_id, agent, roots=None):
    """Publish, prove, amend through coord, and independently read back the binding."""
    if classify_coord_actor(agent)[0] is not CoordActorClass.OPERATOR:
        raise ValueError("source preservation requires an authorized operator")
    if not re.fullmatch(r"[0-9a-f]{8}", card_id):
        raise ValueError("source preservation requires an exact native card id")
    home = Path(home).expanduser()
    card = _coord(home, "show", card_id, "--json")
    original = binding(card)
    revision = original["base_revision"]
    review = _binding(card, "link_source_card") is not None
    if review:
        revision = _binding(card, "link_head_revision")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("review source head binding invalid")
    ref = f"preserve/{card_id}-{revision[:8]}"
    parsed = urlsplit(original["repository"])
    remote = SSH_SOURCE_TRANSPORTS[parsed.hostname] + parsed.path.lstrip("/")
    source = find_source(default_roots(home, card_id) if roots is None else roots, revision)
    if review:
        publish(source, remote, "refs/heads/" + ref, revision, ancestor=original["base_revision"])
        prove_ref(original["repository"], "refs/heads/" + ref, original["base_revision"])
    else:
        publish(source, remote, "refs/heads/" + ref, revision)
    prove_ref(original["repository"], "refs/heads/" + ref, revision)
    before = _coord(home, "show", card_id, "--json")
    if before != card:
        raise ValueError("card changed during source preservation; immutable ref retained")
    if original["base_ref"] != ref:
        _coord(home, "link", card_id, "base_ref", ref, "--agent", agent)
    after = _coord(home, "show", card_id, "--json")
    expected = dict(original, base_ref=ref)
    if binding(after) != expected or any(
        _binding(after, key) != _binding(card, key)
        for key in ("link_source_card", "link_head_revision")
    ):
        raise ValueError("source preservation binding readback differs")
    prove_ref(expected["repository"], "refs/heads/" + ref, revision)
    if review:
        prove_ref(expected["repository"], "refs/heads/" + ref, original["base_revision"])
    return dict(card=card_id, agent=agent, source=str(source), **expected, verified=True)
