"""Run one bounded Seraph private-forge approval cycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .fleet.signing import capauth_signer, own_fingerprint
from .link_observation_feed import load_observation_feed
from .seraph_review_capauth import SERAPH_FINGERPRINT
from .seraph_review_contracts import ReviewPublicationError
from .seraph_review_cycle import publish_ready


def main(argv: list[str] | None = None) -> int:
    """Load one fresh mediated feed and publish its exact eligible reviews."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, required=True)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        signer = capauth_signer()
        if signer is None or own_fingerprint() != SERAPH_FINGERPRINT:
            raise ReviewPublicationError("seraph_signer_unavailable")
        receipts = publish_ready(
            load_observation_feed(args.feed),
            home=args.home,
            evidence_root=args.evidence_root,
            runtime_dir=args.runtime_dir,
            credential_file=args.credential_file,
            signer=signer,
        )
        print(json.dumps({"status": "complete", "published": len(receipts)}, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
