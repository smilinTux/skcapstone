#!/usr/bin/env python3
"""Issue a fleet machine a capauth `node`-class identity, end to end.

Epic 3bbf39ea, card 5ee6510f (parent 804dc9d5). Card fc6500cb built the
ceiling (`capauth.identity_class`); this script is what puts a real node under
it. It was written while doing `.100` by hand, because every step below has a
way to be almost right that fails silently, and the second node should not
have to rediscover them:

* An `attested` enrollment's `attestation` is NOT a detached signature.
  `capauth.crypto.pgpy_backend.PGPyBackend.verify` parses it with
  `PGPMessage.from_blob` and compares the EMBEDDED payload against the
  challenge bytes, so it must be a signed MESSAGE (`gpg --sign`, data plus
  signature in one blob). `gpg --detach-sign` output verifies fine under
  `gpg --verify` and under bare pgpy, and is still rejected here. That is the
  single easiest way to spend an hour on this card.
* The attestation is bound to the device fingerprint AND the CANONICAL
  subject (`capauth.pairing.attested_challenge`). Sign the pre-canonical
  spelling and the enrollment is refused exactly like a forged one.
* The class assignment is STORED (`<base_dir>/identity/classes.json`), never
  passed in the request. A class a caller can assert is a ceiling an attacker
  can raise.
* The scoped token must never carry `Capability.ALL`. The node class forbids
  `*` anyway, so a wildcard would buy nothing but would sit in a
  Syncthing-replicated store waiting for someone to unclassify the subject.

What runs where
---------------
Run this on the box that HOLDS THE OPERATOR SECRET KEY, not on the node. The
node is supposed to have no signing key at all; that is most of the point of
the class. The node's own keypair is generated on the node (see --node-pubkey
and the runbook), and only its PUBLIC half travels.

`~/.skcapstone` is a Syncthing-replicated folder, so the device record, the
class assignment, and the token all reach the node by replication. Nothing is
installed on the node and no service is restarted.

Usage:
    # on the node, once:
    gpg --batch --gen-key <params>       # see docs/fleet/runbook-node-identity.md
    gpg --armor --export node-ollama@chef.skworld.io > node.pub.asc

    # on the operator box:
    python scripts/fleet/issue-node-identity.py \\
        --subject node-ollama@chef.skworld.io \\
        --node-pubkey node.pub.asc \\
        --operator-key BD7EEECA23D90A594400751CFDB582D9CB7272A6

    # add --dry-run to print the plan and verify the attestation without
    # writing anything to the store.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# The scopes a fleet machine legitimately needs: proxy inference plus reads.
# Deliberately a subset of what the `node` identity class allows, so the GRANT
# and the CEILING agree instead of the ceiling silently doing all the work.
# Never `*`, never token:issue, never identity:sign.
DEFAULT_SCOPES = ("skgateway.infer", "skchat.status", "skchat.inbox")

#: 90 days. A node token that never expires is a node token nobody rotates.
DEFAULT_TTL_HOURS = 24 * 90


def build_attestation(operator_key: str, challenge: bytes) -> str:
    """Sign ``challenge`` with ``operator_key`` as a PGP signed MESSAGE.

    `--compress-algo 0` and an empty filename keep the literal packet a plain
    copy of the challenge bytes, which is what the verifier compares against.
    A compressed or text-mode literal is a different byte string and fails the
    embedded-payload comparison rather than the crypto, so it reads like a bad
    signature when it is really a bad envelope.

    Args:
        operator_key: Fingerprint (or any gpg key spec) of the vouching
            operator's SECRET key, which must be present and unlocked here.
        challenge: The exact bytes from ``capauth.pairing.attested_challenge``.

    Returns:
        str: ASCII-armored PGP signed message.

    Raises:
        SystemExit: gpg produced no signature.
    """
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp) / "challenge.bin"
        data.write_bytes(challenge)
        out = Path(tmp) / "attestation.asc"
        result = subprocess.run(
            [
                "gpg", "--batch", "--yes",
                "--local-user", operator_key,
                "--compress-algo", "0",
                "--set-filename", "",
                "--armor", "--sign",
                "-o", str(out), str(data),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0 or not out.exists():
            sys.exit(f"gpg could not sign the attestation with {operator_key}: {result.stderr}")
        return out.read_text(encoding="utf-8")


def export_public_key(key_spec: str) -> str:
    """The ASCII-armored PUBLIC half of ``key_spec`` from the local keyring."""
    result = subprocess.run(
        ["gpg", "--armor", "--export", key_spec],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0 or "BEGIN PGP" not in result.stdout:
        sys.exit(f"gpg could not export a public key for {key_spec}: {result.stderr}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--subject",
        required=True,
        help="Canonical fqid for the node, e.g. node-ollama@chef.skworld.io",
    )
    parser.add_argument(
        "--node-pubkey",
        required=True,
        type=Path,
        help="Path to the node's ASCII-armored PUBLIC key (generated ON the node)",
    )
    parser.add_argument(
        "--operator-key",
        required=True,
        help="Vouching operator's key spec; its SECRET key must be usable here",
    )
    parser.add_argument(
        "--scopes",
        nargs="*",
        default=list(DEFAULT_SCOPES),
        help=f"Token capabilities (default: {' '.join(DEFAULT_SCOPES)})",
    )
    parser.add_argument("--ttl-hours", type=int, default=DEFAULT_TTL_HOURS)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Storage root (default ~/.skcapstone, the replicated store)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify the attestation and print the plan; write nothing",
    )
    args = parser.parse_args(argv)

    from capauth.identity_class import (
        IdentityClassName,
        assign_identity_class,
        load_assignments,
    )
    from capauth.pairing import approve, attested_challenge, enroll_device, list_devices
    from capauth.pairing.kernel import _proof_verifies
    from capauth.pairing.store import default_base_dir, fingerprint_for
    from capauth.subject import canonical_subject
    from capauth.tokens import Capability, issue_token, signature_verifies

    base_dir = args.base_dir if args.base_dir is not None else default_base_dir()

    if Capability.ALL.value in args.scopes:
        sys.exit(
            "refusing to mint a Capability.ALL token for a node identity: the node "
            "class forbids '*', so this grants nothing and only leaves a wildcard "
            "in a replicated store"
        )

    subject = canonical_subject(args.subject)
    node_pub = args.node_pubkey.read_text(encoding="utf-8")
    fingerprint = fingerprint_for(node_pub)
    operator_pub = export_public_key(args.operator_key)

    challenge = attested_challenge(fingerprint, subject)
    attestation = build_attestation(args.operator_key, challenge)
    if not _proof_verifies(operator_pub, attestation, challenge):
        sys.exit(
            "the attestation does not verify against the operator public key. "
            "Most likely it is a DETACHED signature; this path needs a signed "
            "MESSAGE (gpg --sign, not --detach-sign)."
        )

    print(f"subject      : {subject}")
    print(f"node key fp  : {fingerprint}")
    print(f"attested by  : {args.operator_key} (attestation verifies)")
    print(f"scopes       : {args.scopes}")
    print(f"store        : {base_dir}")
    if args.dry_run:
        print("\ndry run: nothing written")
        return 0

    live = [d for d in list_devices(subject, base_dir=base_dir) if not d.revoked]
    if live:
        device = live[0]
        print(f"\ndevice already enrolled: {device.device_id} (mode {device.mode.value})")
    else:
        enrollment = enroll_device(
            pubkey=node_pub,
            requested_scopes=list(args.scopes),
            mode="attested",
            base_dir=base_dir,
            subject=subject,
            operator_id=args.operator_key,
            operator_pubkey=operator_pub,
            attestation=attestation,
        )
        device = approve(enrollment.enrollment_id, args.operator_key, base_dir=base_dir)
        print(f"\nenrolled + approved: {device.device_id} (mode {device.mode.value})")

    stored = assign_identity_class(subject, IdentityClassName.NODE, base_dir=base_dir)
    print(f"identity class : {subject} -> {stored}")
    print(f"classes.json   : {json.dumps(load_assignments(base_dir), sort_keys=True)}")

    token = issue_token(
        home=base_dir,
        subject=subject,
        capabilities=list(args.scopes),
        ttl_hours=args.ttl_hours,
        metadata={"issued_for": subject, "issued_by_script": "issue-node-identity.py"},
    )
    print(
        json.dumps(
            {
                "token_id": token.payload.token_id,
                "issuer": token.payload.issuer,
                "capabilities": token.payload.capabilities,
                "carries_wildcard": Capability.ALL.value in token.payload.capabilities,
                "signature_verifies": signature_verifies(token),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
