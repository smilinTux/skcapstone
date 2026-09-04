"""Same-cycle SKGateway snapshot and lane admission regressions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from skcapstone.fleet_lane_health import (
    MAX_ENDPOINT_BYTES,
    acquire_lane_snapshot,
    active_gateway_revision,
    lane_health,
)

REVISION = "a" * 40
ENDPOINT = "http://gateway.example:18790"
LANES = [
    {"name": "qwen", "model": "qwen-model"},
    {"name": "codex", "model": "sk-codex"},
]
DOMAINS = {"qwen": ("qwen-a", "qwen-b"), "codex": ("codex",)}


class Response:
    def __init__(self, value: dict[str, Any]) -> None:
        self.raw = json.dumps(value).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int) -> bytes:
        return self.raw[:amount]


def _documents(*, codex: str = "up", qwen_a: str = "down") -> dict[str, dict[str, Any]]:
    health = {
        "status": "ok",
        "backends": {
            "qwen-a": {
                "status": qwen_a,
                "observed": True,
                "quarantined": False,
                "lastCheck": 2_000_000_000_000,
            },
            "qwen-b": {
                "status": "up",
                "observed": True,
                "quarantined": False,
                "lastCheck": 2_000_000_000_000,
            },
            "codex": {
                "status": codex,
                "observed": True,
                "quarantined": False,
                "lastCheck": 2_000_000_000_000,
            },
        },
    }
    queue = {
        "pool": {"totalCapacity": 4},
        "timestamp": "2033-05-18T03:33:20Z",
        "backends": {
            name: {"capacityDomain": name, "max": maximum}
            for name, maximum in (("qwen-a", 1), ("qwen-b", 2), ("codex", 1))
        },
    }
    return {"/health": health, "/queue": queue}


def _opener(documents: dict[str, dict[str, Any]], calls: list[str]):
    def open_url(url: str, *, timeout: float) -> Response:
        assert timeout == 5
        calls.append(url)
        path = "/" + url.rsplit("/", 1)[-1]
        value = documents[path]
        if isinstance(value, Exception):
            raise value
        return Response(value)

    return open_url


def _acquire(tmp_path: Path, documents: dict[str, dict[str, Any]], cycle: str = "cycle-1"):
    calls: list[str] = []
    path = tmp_path / "lane-health.json"
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        LANES,
        DOMAINS,
        path,
        cycle,
        opener=_opener(documents, calls),
        revision_resolver=lambda endpoint: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    return snapshot, path, calls


def _admit(snapshot: dict[str, Any], lane: str, model: str, **changes: Any):
    values = {
        "cycle_id": "cycle-1",
        "endpoint": ENDPOINT,
        "capacity_domains": DOMAINS[lane],
        "active_revision": REVISION,
        "now": 2_000_000_001.0,
    }
    values.update(changes)
    return lane_health(snapshot, lane, model, **values)


def test_cold_start_fetches_each_endpoint_once_and_atomically_seals(tmp_path: Path) -> None:
    snapshot, path, calls = _acquire(tmp_path, _documents())
    assert calls == [ENDPOINT + "/health", ENDPOINT + "/queue"]
    assert json.loads(path.read_text()) == snapshot
    assert not list(tmp_path.glob("*.new"))
    assert _admit(snapshot, "qwen", "qwen-model") == (True, "healthy")


def test_active_revision_is_bound_to_configured_endpoint_host_and_port() -> None:
    calls: list[tuple[list[str], str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, stdout=REVISION + "\n", stderr="")

    assert active_gateway_revision(ENDPOINT, runner=runner) == REVISION
    assert calls[0][0][-3:] == ["python3", "-", "18790"]
    assert calls[0][0][0] == "ssh"
    assert "/proc" in calls[0][1]


def test_oversized_endpoint_fails_closed_with_bounded_evidence(tmp_path: Path) -> None:
    class Oversized(Response):
        def read(self, amount: int) -> bytes:
            return b"x" * (MAX_ENDPOINT_BYTES + 1)

    documents = _documents()
    calls: list[str] = []

    def opener(url: str, *, timeout: float) -> Response:
        calls.append(url)
        return Oversized({}) if url.endswith("/health") else Response(documents["/queue"])

    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        LANES,
        DOMAINS,
        tmp_path / "health.json",
        "cycle-1",
        opener=opener,
        revision_resolver=lambda endpoint: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    assert snapshot["errors"] == ["health:ValueError"]
    assert _admit(snapshot, "codex", "sk-codex") == (False, "unknown")


def test_atomic_replacement_removes_previous_cycle(tmp_path: Path) -> None:
    first, path, _ = _acquire(tmp_path, _documents(), "cycle-1")
    first_inode = path.stat().st_ino
    second, _, _ = _acquire(tmp_path, _documents(codex="down"), "cycle-2")
    assert first["cycle_id"] == "cycle-1"
    assert json.loads(path.read_text()) == second
    assert path.stat().st_ino != first_inode


def test_endpoint_capacity_cycle_and_revision_bindings_fail_closed(tmp_path: Path) -> None:
    snapshot, _, _ = _acquire(tmp_path, _documents())
    assert _admit(snapshot, "codex", "sk-codex", endpoint="http://other:18790") == (
        False,
        "endpoint-mismatch",
    )
    assert _admit(snapshot, "codex", "sk-codex", capacity_domains=("other",)) == (
        False,
        "capacity-mismatch",
    )
    assert _admit(snapshot, "codex", "sk-codex", active_revision="b" * 40) == (
        False,
        "revision-mismatch",
    )
    assert _admit(snapshot, "codex", "sk-codex", cycle_id="other") == (
        False,
        "cycle-mismatch",
    )


def test_partial_backend_outage_preserves_independent_healthy_lanes(tmp_path: Path) -> None:
    documents = _documents(codex="down")
    documents["/health"]["backends"].pop("qwen-a")
    snapshot, _, _ = _acquire(tmp_path, documents)
    assert _admit(snapshot, "qwen", "qwen-model") == (True, "healthy")
    assert _admit(snapshot, "codex", "sk-codex") == (
        False,
        "model_owner_backend_down",
    )


def test_endpoint_failure_and_revision_failure_seal_fail_closed_evidence(tmp_path: Path) -> None:
    documents = _documents()
    documents["/health"] = OSError("down")  # type: ignore[assignment]
    calls: list[str] = []
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        LANES,
        DOMAINS,
        tmp_path / "health.json",
        "cycle-1",
        opener=_opener(documents, calls),
        revision_resolver=lambda endpoint: (_ for _ in ()).throw(OSError("missing")),
        now=lambda: 2_000_000_000.0,
    )
    assert calls == [ENDPOINT + "/health", ENDPOINT + "/queue"]
    assert snapshot["errors"] == ["health:OSError", "revision:OSError"]
    assert _admit(snapshot, "codex", "sk-codex") == (False, "revision-mismatch")


def test_rotate_checks_same_cycle_admission_before_claim() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py").read_text()
    acquire = source.index("_lane_health_snapshot=acquire_lane_snapshot(")
    selection = source.index("while _i<len(owned)")
    preclaim = source.index("admitted,health_reason=_health_for(")
    claim = source.index('claim=subprocess.run([SKC,"coord","claim"')
    assert acquire < selection < preclaim < claim


# ---------------------------------------------------------------------------
# Card 0e010300: fleet bootstrap. SKGateway writes a backend health row only
# from proxied request outcomes, so `lastCheck` is when that backend last
# carried traffic, not when the gateway last looked at it. Observation age must
# therefore not gate admission, or the fleet can never be the thing that
# produces its own first observation.
# ---------------------------------------------------------------------------


def _acquire_health(tmp_path: Path, codex_row: dict[str, Any], cycle: str = "cycle-1"):
    documents = _documents()
    documents["/health"]["backends"]["codex"] = codex_row
    snapshot, _path, _calls = _acquire(tmp_path, documents, cycle)
    return snapshot


def _codex_state(snapshot: dict[str, Any]) -> str:
    row = next(item for item in snapshot["lanes"] if item["lane"] == "codex")
    return row["domains"][0]["state"]


def test_idle_but_observed_domain_stays_admissible(tmp_path: Path) -> None:
    """An observed, up, unquarantined domain is admissible however long it idled.

    Measured on the live dispatch gateway 2026-09-04: codex read up/observed
    with a lastCheck five hours old and every lane refused, which meant the
    fleet could not restart itself after two quiet minutes.
    """
    stale = _acquire_health(
        tmp_path,
        {
            "status": "up",
            "observed": True,
            "quarantined": False,
            # Five hours before the pinned observation time of 2_000_000_000.
            "lastCheck": (2_000_000_000 - 5 * 3600) * 1000,
        },
    )
    assert _codex_state(stale) == "healthy"
    assert _admit(stale, "codex", "sk-codex") == (True, "healthy")


def test_unobserved_domain_is_still_refused_after_a_gateway_restart(tmp_path: Path) -> None:
    """The fresh-start state is genuinely no evidence, so it still fails closed."""
    cold = _acquire_health(
        tmp_path,
        {"status": "unknown", "observed": False, "quarantined": False, "lastCheck": 0},
    )
    assert _codex_state(cold) == "unknown"
    assert _admit(cold, "codex", "sk-codex") == (False, "unknown")


def test_malformed_or_future_last_check_still_fails_closed(tmp_path: Path) -> None:
    """Recency is not required, but malformed or impossible evidence is refused."""
    for last_check in (None, "recently", True, 0, -1):
        snapshot = _acquire_health(
            tmp_path,
            {
                "status": "up",
                "observed": True,
                "quarantined": False,
                "lastCheck": last_check,
            },
        )
        assert _codex_state(snapshot) == "unknown", last_check
        assert _admit(snapshot, "codex", "sk-codex") == (False, "unknown"), last_check

    future = _acquire_health(
        tmp_path,
        {
            "status": "up",
            "observed": True,
            "quarantined": False,
            "lastCheck": (2_000_000_000 + 3600) * 1000,
        },
    )
    assert _codex_state(future) == "unknown"
    assert _admit(future, "codex", "sk-codex") == (False, "unknown")


def test_idle_domain_that_is_down_or_quarantined_is_still_refused(tmp_path: Path) -> None:
    """Dropping the recency gate must not admit a domain with negative evidence."""
    idle = (2_000_000_000 - 5 * 3600) * 1000
    down = _acquire_health(
        tmp_path,
        {"status": "down", "observed": True, "quarantined": False, "lastCheck": idle},
    )
    assert _admit(down, "codex", "sk-codex") == (False, "model_owner_backend_down")

    quarantined = _acquire_health(
        tmp_path,
        {"status": "up", "observed": True, "quarantined": True, "lastCheck": idle},
    )
    assert _admit(quarantined, "codex", "sk-codex") == (False, "model_claim_quarantined")


def test_snapshot_freshness_is_still_enforced_independently(tmp_path: Path) -> None:
    """Observation age is not bounded; the snapshot's own age still is."""
    snapshot = _acquire_health(
        tmp_path,
        {
            "status": "up",
            "observed": True,
            "quarantined": False,
            "lastCheck": (2_000_000_000 - 5 * 3600) * 1000,
        },
    )
    assert _admit(snapshot, "codex", "sk-codex") == (True, "healthy")
    assert _admit(snapshot, "codex", "sk-codex", now=2_000_000_600.0) == (False, "stale")
