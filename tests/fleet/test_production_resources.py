"""Resource accounting must permit useful concurrency without host overcommit."""

import pytest

from skcapstone.fleet.production_resources import available_worker_memory


def test_memory_reservations_subtract_only_unconsumed_allowance():
    evidence = "MemTotal: 10000000 kB\nMemAvailable: 6000000 kB\n"
    units = [{"MemoryMax": str(3000000 * 1024), "MemoryCurrent": str(1000000 * 1024)}]
    assert available_worker_memory(evidence, units) == 3000000 * 1024
    assert available_worker_memory(evidence, []) == 5000000 * 1024


@pytest.mark.parametrize("maximum", ["infinity", "0", str(2**64 - 1)])
def test_unknown_existing_reservation_is_not_free_capacity(maximum):
    with pytest.raises(ValueError):
        available_worker_memory(
            "MemTotal: 8000000 kB\nMemAvailable: 7000000 kB\n",
            [{"MemoryMax": maximum, "MemoryCurrent": "0"}],
        )


def test_missing_memory_measurement_is_not_zero_usage():
    with pytest.raises(ValueError):
        available_worker_memory("MemTotal: 8000000 kB\n", [])
