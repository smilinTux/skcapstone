"""Prove an operator's ref-only amendment without changing the source bytes."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card_store import CardStore

from .seat_boundaries import CoordActorClass, classify_coord_actor
from .source_binding import source_binding_meta

_LOG = logging.getLogger(__name__)
_FIELDS = ("repository", "base_ref", "base_revision")


def _timestamp(value):
    """Require an aware timestamp rather than infer the host timezone."""
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("source amendment timestamp must be timezone aware")
    return stamp


def _evidence(core, home):
    """Read the current native binding and the winning base_ref link event."""
    cid = core.get("id")
    if not isinstance(cid, str) or not re.fullmatch(r"[0-9a-f]{8}", cid):
        raise ValueError("source binding conflict: base_ref requires a native card")
    store = CardStore(home)
    card = store.fold(cid)
    meta, links = core.get("meta") or {}, core.get("links") or {}
    if card is None or any(
        (meta.get(key), links.get(key)) != (card.meta.get(key), card.links.get(key))
        for key in _FIELDS
    ):
        raise ValueError("source amendment differs from native binding")
    for key in ("repository", "base_revision"):
        if not meta.get(key) or links.get(key, meta[key]) != meta[key]:
            raise ValueError("source amendment requires unchanged repository and base_revision")
    source_binding_meta([], meta["repository"], meta.get("base_ref"), meta["base_revision"])
    ref = links.get("base_ref")
    binding = source_binding_meta([], meta["repository"], ref, meta["base_revision"])
    if not meta.get("base_ref") or ref == meta["base_ref"]:
        raise ValueError("source amendment requires differing named refs")
    events = store._read_events(cid) + store._legacy_events(cid)
    links_to_ref = [
        event
        for event in events
        if event.get("action") == "link" and event.get("link_key") == "base_ref"
    ]
    if not links_to_ref:
        raise ValueError("source amendment requires an operator link event")
    event = max(
        links_to_ref, key=lambda row: (row.get("ts", ""), row.get("writer", ""), row.get("seq", 0))
    )
    writer = event.get("writer", "")
    if (
        not isinstance(writer, str)
        or any(char in writer for char in "\r\n|")
        or classify_coord_actor(writer)[0] is not CoordActorClass.OPERATOR
    ):
        raise ValueError("source amendment requires an authorized operator, not a worker")
    if event.get("link_value") != ref:
        raise ValueError("source amendment native link event differs")
    if not _timestamp(card.created_at) < _timestamp(event.get("ts")) <= datetime.now(timezone.utc):
        raise ValueError("source amendment timestamp must follow creation and not be future")
    return binding, meta["base_ref"], writer, event


def prove_ref(repository, ref, revision):
    """Fetch a named ref with sanitized Git settings and prove exact ancestry."""
    from urllib.parse import urlsplit

    from .fleet.builder_dispatch import SSH_SOURCE_TRANSPORTS

    source_binding_meta([], repository, ref, revision)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
    }
    git = ["/usr/bin/git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null"]
    host = urlsplit(repository).hostname
    if host in SSH_SOURCE_TRANSPORTS:
        env["HOME"] = str(Path.home())
        env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
        git += ["-c", f"url.{SSH_SOURCE_TRANSPORTS[host]}.insteadOf=https://{host}/"]

    def run(args, *, cwd=None):
        result = subprocess.run(
            [*git, *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            raise ValueError("source amended ref fetch or ancestry proof failed")
        return result.stdout

    candidates = [ref] if ref.startswith("refs/") else [f"refs/heads/{ref}", f"refs/tags/{ref}"]
    try:
        advertised = run(["ls-remote", "--exit-code", repository, *candidates]).splitlines()
        rows = [line.split("\t") for line in advertised]
        if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] not in candidates:
            raise ValueError("source amended ref is missing or ambiguous")
        with tempfile.TemporaryDirectory(prefix="skfleet-source-ref-") as directory:
            run(["init", "--bare", "--quiet", directory])
            run(["fetch", "--quiet", "--no-tags", repository, rows[0][1]], cwd=directory)
            if run(["rev-parse", "FETCH_HEAD"], cwd=directory).strip() != rows[0][0]:
                raise ValueError("source amended ref changed during fetch")
            run(["merge-base", "--is-ancestor", revision, "FETCH_HEAD"], cwd=directory)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("source amended ref proof unavailable") from exc


def amended_source_ref(core, *, home=None, emit=None):
    """Accept only an attributed, unchanged-source, currently fetchable amendment."""
    home = Path(home or os.environ.get("SKCAPSTONE_HOME", Path.home() / ".skcapstone"))
    before = _evidence(core, home)
    binding, original, writer, _event = before
    prove_ref(binding["repository"], binding["base_ref"], binding["base_revision"])
    if _evidence(core, home) != before:
        raise ValueError("source amendment changed during proof")
    (emit or _LOG.info)(
        f"SOURCE_BINDING_AMENDED|{core['id']}|{original}|{binding['base_ref']}|{writer}"
    )
    return binding["base_ref"]
