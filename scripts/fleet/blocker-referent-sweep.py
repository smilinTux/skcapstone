#!/usr/bin/env python3
"""Return cards whose recorded blocker has since completed.

Reports by default. Pass --go to actually label the cards, because returning a
card puts it back in front of a worker and that should be deliberate.

    blocker-referent-sweep.py            # report only
    blocker-referent-sweep.py --go       # label them blocker-now-done

See skcapstone.blocker_referent for why this exists and what it measured.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from skcapstone.blocker_referent import card_dir_lookup, find_returnable  # noqa: E402

LABEL = "blocker-now-done"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--go", action="store_true", help="label the cards")
    parser.add_argument("--agent", default=os.environ.get("SKAGENT", "lumina"))
    parser.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    args = parser.parse_args()

    home = Path(args.home)
    try:
        from skcoord.card_store import CardStore
    except ImportError:
        print("skcoord is not importable; run this where the board lives", file=sys.stderr)
        return 2

    store = CardStore(home)
    lookup = card_dir_lookup(home)

    def is_done(prefix: str):
        name = lookup(prefix)
        if name is None:
            return None
        try:
            return str(getattr(store.fold(name), "status", "")) == "Column.DONE"
        except Exception:
            return None

    def is_open(card_id: str) -> bool:
        return is_done(str(card_id)[:8]) is False

    returnable, still_blocked, missing = find_returnable(home, is_done, is_open)

    print("blocker-referent sweep")
    print("  returnable (every cited blocker is DONE): %d" % len(returnable))
    print("  still genuinely blocked:                  %d" % still_blocked)
    print("  citing a card that does not exist:        %d" % missing)

    if not returnable:
        return 0
    if not args.go:
        for card_id in returnable:
            print("    would return %s" % card_id)
        print("  dry run; pass --go to label these %d" % len(returnable))
        return 0

    labelled = 0
    for card_id in returnable:
        result = subprocess.run(
            ["skcapstone", "coord", "label", card_id, LABEL, "--agent", args.agent],
            capture_output=True,
        )
        if result.returncode == 0:
            labelled += 1
        else:
            print("    could not label %s" % card_id, file=sys.stderr)
    print("  labelled %d of %d" % (labelled, len(returnable)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
