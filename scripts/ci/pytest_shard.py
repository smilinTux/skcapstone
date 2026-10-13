"""Deterministic file-level test sharding for CI (``-p pytest_shard --shard I/N``).

Whole files are assigned to shards, largest first, each to the currently
lightest shard (weight = measured seconds from shard-weights.json, else tests * 0.15s). Assignment
depends only on the collected node ids, so the pytest controller and every
pytest-xdist worker compute the same partition, and the N shards together
select every collected test exactly once.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Measured seconds per test file (sum of testcase times from a green CI run).
# Files absent from the table fall back to ``tests * _DEFAULT_SECONDS_PER_TEST``.
# Stale numbers only unbalance the shards; they can never drop or duplicate a
# test, so refreshing this file is an optimisation, not a correctness matter.
WEIGHTS_FILE = Path(__file__).with_name("shard-weights.json")
_DEFAULT_SECONDS_PER_TEST = 0.15


def load_weights(path: Path = WEIGHTS_FILE) -> dict[str, float]:
    try:
        return {str(k): float(v) for k, v in json.loads(path.read_text("utf-8")).items()}
    except (OSError, ValueError):
        return {}


def parse_shard(spec: str) -> tuple[int, int]:
    """Parse ``"I/N"`` (1-based) into ``(index0, count)``."""
    index, _, count = spec.partition("/")
    i, n = int(index), int(count)
    if n < 1 or not 1 <= i <= n:
        raise ValueError(f"invalid shard {spec!r}; expected I/N with 1 <= I <= N")
    return i - 1, n


def assign(weights: dict[str, float], count: int) -> dict[str, int]:
    """Map each file to a shard index, balancing total weight (LPT greedy)."""
    loads = [0.0] * count
    owner: dict[str, int] = {}
    for path in sorted(weights, key=lambda p: (-weights[p], p)):
        shard = min(range(count), key=lambda k: (loads[k], k))
        owner[path] = shard
        loads[shard] += weights[path]
    return owner


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--shard", default=None, metavar="I/N", help="run only shard I of N (by file)"
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = config.getoption("--shard")
    if not spec:
        return
    index, count = parse_shard(spec)
    counts: dict[str, int] = {}
    for item in items:
        path = item.nodeid.split("::", 1)[0]
        counts[path] = counts.get(path, 0) + 1
    measured = load_weights()
    weights = {
        path: measured.get(path, n * _DEFAULT_SECONDS_PER_TEST) for path, n in counts.items()
    }
    owner = assign(weights, count)
    keep = [item for item in items if owner[item.nodeid.split("::", 1)[0]] == index]
    dropped = [item for item in items if owner[item.nodeid.split("::", 1)[0]] != index]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
    items[:] = keep
