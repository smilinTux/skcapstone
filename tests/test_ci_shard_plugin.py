"""The CI shard partition must cover every file exactly once, deterministically."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts" / "ci"))
import pytest_shard  # noqa: E402


def test_assign_partitions_every_file_once_and_is_deterministic() -> None:
    weights = {f"tests/test_{i}.py": (i * 7) % 23 + 1 for i in range(60)}
    first = pytest_shard.assign(weights, 4)
    assert first == pytest_shard.assign(dict(reversed(list(weights.items()))), 4)
    assert set(first) == set(weights)
    assert set(first.values()) == {0, 1, 2, 3}
    loads = [sum(w for p, w in weights.items() if first[p] == k) for k in range(4)]
    assert max(loads) - min(loads) <= max(weights.values())


@pytest.mark.parametrize("spec", ["0/4", "5/4", "1/0", "x"])
def test_parse_shard_rejects_bad_specs(spec: str) -> None:
    with pytest.raises(ValueError):
        pytest_shard.parse_shard(spec)


def test_parse_shard() -> None:
    assert pytest_shard.parse_shard("1/4") == (0, 4)
    assert pytest_shard.parse_shard("4/4") == (3, 4)
