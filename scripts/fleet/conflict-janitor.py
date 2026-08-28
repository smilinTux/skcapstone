#!/usr/bin/env python3
"""Remove Syncthing conflict copies that provably carry nothing unique.

Syncthing writes `<name>.sync-conflict-<date>-<device>.<ext>` beside a file when
two devices changed it independently. In this estate ~/.skcapstone is one shared
folder written continuously by five hosts plus named agents, so conflicts are
routine and nothing ever removed them. They accumulate without bound, and they
are not inert: skmail read them as separate mailboxes and replayed old mail from
them, and card_events conflict copies are scanned by every reader of the
evidence store.

THE RULE IS DELETE ONLY WHAT IS PROVABLY REDUNDANT.

  *.jsonl   append-only line stores. The copy is redundant when every one of its
            non-empty lines already appears in the canonical file. Line order
            does not matter; presence does.
  other     compared byte-for-byte. Redundant only when identical.

A copy carrying even one line the canonical file lacks is KEPT and reported. That
is real divergence and deleting it would destroy the only record of it. This is
the whole point: an unbounded pile of conflicts is a nuisance, but silently
dropping a line that exists nowhere else is data loss.

Dry run by default. Pass --apply to remove.
"""
import hashlib
import os
import re
import sys

CONFLICT_RE = re.compile(r"^(?P<base>.+?)\.sync-conflict-\d{8}-\d{6}-[A-Z0-9]+(?P<ext>\..+)?$")


def canonical_for(path):
    d, name = os.path.split(path)
    m = CONFLICT_RE.match(name)
    if not m:
        return None
    return os.path.join(d, m.group("base") + (m.group("ext") or ""))


def lines_of(path):
    out = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.add(line)
    except OSError:
        return None
    return out


def sha(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def classify(conflict):
    canon = canonical_for(conflict)
    if not canon or not os.path.exists(canon):
        return "no-canonical", 0
    if conflict.endswith(".jsonl"):
        a, b = lines_of(conflict), lines_of(canon)
        if a is None or b is None:
            return "unreadable", 0
        missing = a - b
        return ("redundant", 0) if not missing else ("unique-lines", len(missing))
    sa, sb = sha(conflict), sha(canon)
    if sa is None or sb is None:
        return "unreadable", 0
    return ("redundant", 0) if sa == sb else ("differs", 1)


def main():
    root = os.path.expanduser("~/.skcapstone")
    apply_ = "--apply" in sys.argv
    counts = {}
    freed = 0
    keep_examples = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if ".sync-conflict-" not in fn:
                continue
            p = os.path.join(dirpath, fn)
            verdict, n = classify(p)
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict == "redundant":
                try:
                    size = os.path.getsize(p)
                except OSError:
                    size = 0
                if apply_:
                    try:
                        os.remove(p)
                        freed += size
                    except OSError:
                        counts["delete-failed"] = counts.get("delete-failed", 0) + 1
                else:
                    freed += size
            elif len(keep_examples) < 6:
                keep_examples.append((verdict, n, os.path.relpath(p, root)))
    print("    mode: %s" % ("APPLY" if apply_ else "dry run"))
    for k in sorted(counts):
        print("    %-14s %d" % (k, counts[k]))
    print("    %s %.1f MB" % ("freed" if apply_ else "would free", freed / 1048576.0))
    if keep_examples:
        print("    KEPT, carrying content the canonical file lacks:")
        for v, n, rel in keep_examples:
            extra = (" (%d unique lines)" % n) if n else ""
            print("      %-12s %s%s" % (v, rel[:74], extra))


if __name__ == "__main__":
    main()
