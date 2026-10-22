"""Bounded native candidate custody over the existing trusted SSH channel."""

import base64
import getpass
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

from . import source_bundle as bundle

MAX_PACKET = 2 * (bundle.MAX_BUNDLE + bundle.MAX_EVIDENCE)


def _packet(home: Path, card: str, head: str) -> dict:
    """Read only one immutable source-only candidate's existing custody."""
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise bundle.SourceBundleError("transport head invalid")
    root = bundle._root(home, card)
    manifest = json.loads(bundle._read(root / (head + ".json"), bundle.MAX_EVIDENCE))
    digest = manifest.get("bundle_sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(
        r"[0-9a-f]{64}", str(manifest.get("evidence_sha256", ""))
    ):
        raise bundle.SourceBundleError("transport bundle digest invalid")
    evidence = bundle._read(root / (manifest["evidence_sha256"] + ".md"), bundle.MAX_EVIDENCE)
    blob = bundle._read(root / (digest + ".bundle"), bundle.MAX_BUNDLE)
    if (
        manifest.get("card") != card
        or manifest.get("head") != head
        or bundle._sha(blob) != digest
        or len(blob) != manifest.get("bundle_bytes")
        or bundle._sha(evidence) != manifest["evidence_sha256"]
    ):
        raise bundle.SourceBundleError("retained transport artifacts changed")
    return {
        "manifest": manifest,
        "bundle": base64.b64encode(blob).decode(),
        "evidence": base64.b64encode(evidence).decode(),
    }


def _retain(home: Path, packet: dict, *, authoritative: bool) -> str:
    """Validate immutable bytes and, at authority, current native producer custody."""
    manifest = packet["manifest"]
    if not isinstance(manifest, dict) or manifest.get("schema") != "skfleet.source-bundle/v1":
        raise bundle.SourceBundleError("transport manifest invalid")
    for key, width in (
        ("head", 40),
        ("tree", 40),
        ("base_revision", 40),
        ("bundle_sha256", 64),
        ("evidence_sha256", 64),
    ):
        if not re.fullmatch(r"[0-9a-f]{" + str(width) + "}", str(manifest.get(key, ""))):
            raise bundle.SourceBundleError("transport manifest hash invalid")
    root = bundle._root(home, manifest["card"])
    raw = base64.b64decode(packet["bundle"], validate=True)
    evidence = base64.b64decode(packet["evidence"], validate=True)
    if (
        not 0 < len(raw) <= bundle.MAX_BUNDLE
        or len(raw) != manifest["bundle_bytes"]
        or bundle._sha(raw) != manifest["bundle_sha256"]
        or not 0 < len(evidence) <= bundle.MAX_EVIDENCE
        or bundle._sha(evidence) != manifest["evidence_sha256"]
    ):
        raise bundle.SourceBundleError("transport artifact bytes differ")
    if authoritative:
        from skcoord.card_store import CardStore, card_mutation_lock

        from ..seraph_review_cardstore import _latest_outcome

        with card_mutation_lock(home, manifest["card"]):
            store = CardStore(home)
            row = store.fold(manifest["card"])
            outcome = _latest_outcome(store, manifest["card"])
            if (
                row is None
                or "source-only" not in row.labels
                or row.owner != manifest["owner"]
                or row.meta.get("_claim_revision") != manifest["claim_revision"]
                or outcome.get("action") != "verdict"
                or outcome.get("verdict") != "PASS_FOR_REVIEW"
                or outcome.get("writer") != manifest["owner"]
                or outcome.get("expected_claim_revision") not in (None, manifest["claim_revision"])
            ):
                raise bundle.SourceBundleError("authority candidate generation unavailable")
            claims = [
                event
                for event in store._read_events(manifest["card"])
                if event.get("action") == "claim"
                and (event.get("claim_revision") or event.get("event_id"))
                == manifest["claim_revision"]
            ]
            if len(claims) != 1 or str(outcome.get("ts", "")) < str(claims[0].get("ts", "")):
                raise bundle.SourceBundleError("authority proposal predates claim")
            for key, expected in (
                ("candidate_commit", manifest["head"]),
                ("candidate_tree", manifest["tree"]),
                ("candidate_ref", manifest["ref"]),
                ("candidate_sha256", manifest["evidence_sha256"]),
            ):
                if outcome.get(key) != expected:
                    raise bundle.SourceBundleError("authority proposal differs from transfer")
            for key in ("repository", "base_revision"):
                if bundle._binding({"meta": row.meta, "links": row.links}, key) != manifest[key]:
                    raise bundle.SourceBundleError("authority source binding differs")
            target = Path(str(outcome.get("candidate_path", "")))
            if root.parent not in target.parents:
                raise bundle.SourceBundleError("authority candidate evidence path invalid")
            bundle._once(target, evidence)
    bundle._once(root / (manifest["bundle_sha256"] + ".bundle"), raw)
    bundle._once(root / (manifest["evidence_sha256"] + ".md"), evidence)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    bundle._once(root / (manifest["head"] + ".json"), encoded)
    return bundle._sha(encoded)


def _authority() -> str | None:
    """Resolve only the validated production authority, never a packet hostname."""
    from .production_policy import load_production_policy

    path = os.environ.get("SKFLEET_PRODUCTION_POLICY")
    if path is None:
        return None
    authority = os.environ.get("SKFLEET_AUTHORITY_HOST", "")
    if not path or not authority:
        raise bundle.SourceBundleError("artifact production authority missing")
    value = load_production_policy(Path(path), host=authority)
    host = value["authority_host"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,100}", host):
        raise bundle.SourceBundleError("artifact authority invalid")
    return host


def _rpc(authority: str, payload: dict) -> dict:
    """Use existing user identity and strict trusted host keys; capture all bytes."""
    user = getpass.getuser()
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
        raise bundle.SourceBundleError("artifact transport account invalid")
    raw = json.dumps({"authority": authority, **payload}, separators=(",", ":")).encode()
    if len(raw) > MAX_PACKET:
        raise bundle.SourceBundleError("artifact transfer exceeds bound")
    remote = "~/.skenv/bin/python -m skcapstone.fleet.source_transport"
    try:
        result = subprocess.run(
            [
                "ssh",
                "-T",
                "-oBatchMode=yes",
                "-oConnectTimeout=5",
                "-oStrictHostKeyChecking=yes",
                "-oClearAllForwardings=yes",
                user + "@" + authority,
                remote,
            ],
            input=raw,
            capture_output=True,
            timeout=60,
        )
        if result.returncode or not 0 < len(result.stdout) <= MAX_PACKET:
            raise ValueError("transport unavailable")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise ValueError("transport object required")
        return value
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise bundle.SourceBundleError("native artifact authority transfer unavailable") from exc


def push(home: Path, manifest: dict) -> None:
    """Publish only a machine-qualified producer candidate to native authority."""
    authority = _authority()
    if authority is None or authority == socket.gethostname().split(".")[0].lower():
        return
    packet = _packet(home, manifest["card"], manifest["head"])
    expected = bundle._sha(
        json.dumps(packet["manifest"], sort_keys=True, separators=(",", ":")).encode()
    )
    if _rpc(authority, {"action": "put", "packet": packet}).get("manifest_sha256") != expected:
        raise bundle.SourceBundleError("artifact authority acknowledgment differs")


def pull(home: Path, card: str, head: str) -> None:
    """Retrieve exact public candidate artifacts without broad evidence replication."""
    authority = _authority()
    if authority is None or authority == socket.gethostname().split(".")[0].lower():
        return
    packet = _rpc(authority, {"action": "get", "card": card, "head": head})
    if packet.get("manifest", {}).get("card") != card or packet["manifest"].get("head") != head:
        raise bundle.SourceBundleError("artifact authority returned another candidate")
    _retain(home, packet, authoritative=False)


def serve() -> None:
    """Bounded SSH protocol handler; never expose errors, unrelated files or secrets."""
    try:
        raw = sys.stdin.buffer.read(MAX_PACKET + 1)
        if not 0 < len(raw) <= MAX_PACKET:
            raise ValueError("packet bound")
        request = json.loads(raw)
        if request["authority"] != socket.gethostname().split(".")[0].lower():
            raise ValueError("authority mismatch")
        home = Path.home() / ".skcapstone"
        if request["action"] == "put":
            response = {"manifest_sha256": _retain(home, request["packet"], authoritative=True)}
        elif request["action"] == "get":
            from skcoord.card_store import CardStore

            from ..seraph_review_cardstore import _latest_outcome

            bundle._root(home, request["card"])
            if not re.fullmatch(r"[0-9a-f]{40}", str(request["head"])):
                raise ValueError("exact candidate head required")
            store = CardStore(home)
            row = store.fold(request["card"])
            outcome = _latest_outcome(store, request["card"])
            if (
                row is None
                or "source-only" not in row.labels
                or row.archived
                or outcome.get("candidate_commit") != request["head"]
                or outcome.get("action") != "verdict"
                or outcome.get("verdict") not in {"PASS_FOR_REVIEW", "PASS"}
            ):
                raise ValueError("current source-only candidate required")
            response = _packet(home, request["card"], request["head"])
        else:
            raise ValueError("unsupported action")
        sys.stdout.write(json.dumps(response, separators=(",", ":")))
    except Exception:
        sys.exit(1)


if __name__ == "__main__":
    serve()
