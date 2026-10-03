#!/usr/bin/env python3
"""Run the exact selected pytest files in bounded processes, preserving failures."""

import argparse
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true")
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("paths", nargs="+")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("batch size must be positive")
    common = ["--strict-markers", "-m", "not integration and not e2e"]
    pytest = [sys.executable, "-m", "pytest"]
    collected = subprocess.run(
        [*pytest, *args.paths, *common, "--collect-only", "-q", "-rs", "-o", "addopts="],
        capture_output=True,
        text=True,
    )
    if collected.returncode:
        print(collected.stdout, flush=True)
        print(collected.stderr, file=sys.stderr, flush=True)
        return collected.returncode
    nodeids = [
        line
        for line in collected.stdout.splitlines()
        if "::" in line and Path(line.split("::", 1)[0]).is_file()
    ]
    # Keep collection-time skips and warnings visible as well as batch results.
    selected = set(nodeids)
    for line in collected.stdout.splitlines():
        if line not in selected:
            print(line, flush=True)
    files = list(dict.fromkeys(node.split("::", 1)[0] for node in nodeids))
    if not files:
        print("No test files collected; refusing an empty success.", file=sys.stderr)
        return 5
    print(
        f"Selected {len(nodeids)} cases in {len(files)} files; every file runs once.", flush=True
    )
    failed = False
    for offset in range(0, len(files), args.batch_size):
        batch = files[offset : offset + args.batch_size]
        print(f"Batch {offset // args.batch_size + 1}: {batch[0]} through {batch[-1]}", flush=True)
        print(
            f"::notice title=pytest batch {offset // args.batch_size + 1}::Starting {len(batch)} files",
            flush=True,
        )
        coverage = ["--cov=skcapstone", "--cov-report="] if args.coverage else []
        if args.coverage and offset:
            coverage.append("--cov-append")
        result = subprocess.run(
            [*pytest, *batch, *common, *coverage, "-o", "faulthandler_timeout=120"]
        )
        print(f"Batch completed with exit {result.returncode}", flush=True)
        print(
            f"::notice title=pytest batch {offset // args.batch_size + 1}::Exit status {result.returncode}",
            flush=True,
        )
        failed |= result.returncode != 0
    if args.coverage:
        for report in ("xml", "report"):
            result = subprocess.run([sys.executable, "-m", "coverage", report])
            failed |= result.returncode != 0
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
