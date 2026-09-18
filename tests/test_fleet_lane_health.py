"""Same-cycle SKGateway snapshot and lane admission regressions."""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from skcapstone.fleet_lane_health import (
    ENDPOINT_TIMEOUT_SECONDS,
    MAX_ENDPOINT_BYTES,
    acquire_lane_snapshot,
    active_gateway_revision,
    gateway_root,
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
        assert timeout == ENDPOINT_TIMEOUT_SECONDS == 8
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


@pytest.mark.parametrize("size", ["S", "M", "L", "XL"])
@pytest.mark.parametrize("overrides", [None, "distinct", "shared"])
@pytest.mark.parametrize("backend_status", ["up", "down"])
def test_rotator_codex_size_aliases_have_exact_health_admission(
    tmp_path: Path, monkeypatch, size: str, overrides: str | None, backend_status: str
) -> None:
    """Configured role aliases share capacity health without duplicate bindings."""
    for level in ("S", "M", "L", "XL"):
        monkeypatch.delenv("SKFLEET_CODEX_MODEL_" + level, raising=False)
        key = "SKFLEET_MODEL_" + level
        monkeypatch.delenv(key, raising=False)
        if overrides:
            monkeypatch.setenv(key, "custom-" + (level if overrides == "distinct" else "shared"))
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    names = {"_LOGICAL_ROUTES", "_SIZE_MODEL_DEFAULTS", "_SIZE_MODELS", "_GLM_SIZE_RE"}
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
        or isinstance(node, ast.FunctionDef)
        and node.name in {"_size_model_for", "_lane_model"}
    ]
    start = next(
        i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_health_lanes" for t in node.targets)
    )
    end = next(i for i in range(start + 1, len(tree.body)) if isinstance(tree.body[i], ast.Assign))
    namespace = {
        "os": os,
        "re": re,
        "LANES": [{"name": "codex", "model": "sk-codex-mid"}, {"name": "glm", "model": "glm-4.6"}],
        "_GLM_LEVELS": {"S": "glm-4.6", "XL": "glm-5.3"},
    }
    exec(
        compile(
            ast.Module(body=body + tree.body[start:end], type_ignores=[]), str(script), "exec"
        ),
        namespace,
    )
    lanes = namespace["_health_lanes"]
    codex_models = [lane["model"] for lane in lanes if lane["name"] == "codex"]
    assert len(codex_models) == len(set(codex_models))
    assert set(codex_models) == {"sk-codex-mid", *namespace["_SIZE_MODELS"].values()}
    model = namespace["_lane_model"](namespace["LANES"][0], {"title": f"[{size}] Work"})
    assert model == namespace["_SIZE_MODELS"][size]
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        lanes,
        {"codex": ("codex",), "glm": ("zai",)},
        tmp_path / "health.json",
        "cycle-1",
        opener=_opener(_documents(codex=backend_status), []),
        revision_resolver=lambda endpoint: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    expected = (True, "healthy") if backend_status == "up" else (False, "model_owner_backend_down")
    assert _admit(snapshot, "codex", model) == expected
    assert _admit(snapshot, "codex", "unconfigured-alias") == (False, "model-mismatch")


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
    selection = source.index("while _i<len(owned) and _i<len(_candidate_scan)")
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


# ---------------------------------------------------------------------------
# Regression: a base URL carrying the OpenAI-compatible /v1 prefix.
#
# On 2026-09-18 all three chi rotate hosts had
# SKFLEET_GATEWAY_URL=http://<host>:18790/v1, so the probe requested
# /v1/health and /v1/queue. Both 404, both became HTTPError, every lane went
# "unknown", and lane admission (fail-closed by design) blocked every card.
# The fleet had not launched a worker in three days while the gateway was
# healthy throughout.
#
# The pre-existing _opener mock could not catch this: it resolves a request
# by taking only the final path segment (`"/" + url.rsplit("/", 1)[-1]`), so
# ".../v1/health" and ".../health" are indistinguishable to it. The strict
# opener below routes on the FULL path, the way a real server does.
# ---------------------------------------------------------------------------


def _strict_opener(documents: dict[str, dict[str, Any]], calls: list[str]):
    """Route on the full URL path, and 404 anything that is not an exact hit."""

    def open_url(url: str, *, timeout: float) -> Response:
        calls.append(url)
        path = urllib.parse.urlsplit(url).path.rstrip("/") or "/"
        if path not in documents:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        value = documents[path]
        if isinstance(value, Exception):
            raise value
        return Response(value)

    return open_url


def test_gateway_root_discards_any_path_component() -> None:
    assert gateway_root("http://chiap01:18790/v1") == "http://chiap01:18790"
    assert gateway_root("http://chiap01:18790/v1/") == "http://chiap01:18790"
    assert gateway_root("http://chiap01:18790") == "http://chiap01:18790"
    assert gateway_root("  http://chiap01:18790/v1  ") == "http://chiap01:18790"
    assert gateway_root("https://gw.example/v1/extra") == "https://gw.example"


def test_gateway_root_leaves_an_undecomposable_value_alone() -> None:
    """Never invent an origin out of something that is not a URL."""
    assert gateway_root("not-a-url") == "not-a-url"
    assert gateway_root("localhost:18790/") == "localhost:18790"


def test_v1_suffixed_base_url_still_probes_the_root_endpoints(tmp_path: Path) -> None:
    """The production failure, reproduced end to end with a strict server."""
    calls: list[str] = []
    snapshot = acquire_lane_snapshot(
        ENDPOINT + "/v1",
        LANES,
        DOMAINS,
        tmp_path / "lane-health.json",
        "cycle-v1",
        opener=_strict_opener(_documents(), calls),
        revision_resolver=lambda _base: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    assert calls == [ENDPOINT + "/health", ENDPOINT + "/queue"], calls
    assert snapshot["errors"] == []
    assert snapshot["endpoint"] == ENDPOINT
    healthy = [domain["state"] for lane in snapshot["lanes"] for domain in lane["domains"]]
    assert "healthy" in healthy, healthy


def test_strict_opener_would_have_caught_the_bug(tmp_path: Path) -> None:
    """Guard the guard: prove the strict opener actually 404s a /v1 path, so
    this regression test cannot silently start passing for the wrong reason.
    """
    calls: list[str] = []
    opener = _strict_opener(_documents(), calls)
    with pytest.raises(urllib.error.HTTPError):
        opener(ENDPOINT + "/v1/health", timeout=8)


def test_a_v1_base_url_is_admissible_end_to_end_not_just_sealed(tmp_path: Path) -> None:
    """The blocker this test exists to prevent.

    Normalizing only inside acquire_lane_snapshot seals the ROOT into
    snapshot["endpoint"] while callers still pass the RAW env value to
    lane_health, whose endpoint comparison then fails. That does not fix the
    outage, it relabels it: every lane is refused with "endpoint-mismatch"
    instead of "unknown", and because the probe now succeeds, errors is []
    and the one signal that exposed the original 373-NOOP outage is gone.

    So the assertion has to reach admissibility, not stop at the snapshot.
    """
    calls: list[str] = []
    snapshot = acquire_lane_snapshot(
        ENDPOINT + "/v1",
        LANES,
        DOMAINS,
        tmp_path / "lane-health.json",
        "cycle-v1-admit",
        opener=_strict_opener(_documents(), calls),
        revision_resolver=lambda _base: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    assert snapshot["errors"] == []

    # The raw, /v1-suffixed value is what a caller actually holds.
    admitted, reason = lane_health(
        snapshot,
        "codex",
        "sk-codex",
        cycle_id="cycle-v1-admit",
        endpoint=ENDPOINT + "/v1",
        capacity_domains=DOMAINS["codex"],
        active_revision=REVISION,
        now=2_000_000_000.0,
    )
    assert (admitted, reason) == (
        True,
        "healthy",
    ), f"a /v1 base URL must be admissible end to end, got {(admitted, reason)}"


def test_the_root_form_is_still_admissible(tmp_path: Path) -> None:
    """Complement: normalizing must not break the correct form."""
    calls: list[str] = []
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        LANES,
        DOMAINS,
        tmp_path / "lh.json",
        "cycle-root",
        opener=_strict_opener(_documents(), calls),
        revision_resolver=lambda _base: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    admitted, reason = lane_health(
        snapshot,
        "codex",
        "sk-codex",
        cycle_id="cycle-root",
        endpoint=ENDPOINT,
        capacity_domains=DOMAINS["codex"],
        active_revision=REVISION,
        now=2_000_000_000.0,
    )
    assert (admitted, reason) == (True, "healthy")


def test_duplicate_identical_lane_bindings_are_one_observation(tmp_path: Path) -> None:
    """A repeated (lane, model) binding must not read as ambiguous evidence.

    Measured live on chi, 2026-09-18 04:01:14 CDT: the rotator's health-lane
    list carried (kimi, kimi-for-coding) twice, once from LANES and once from
    the unconditional kimi alias append, so every snapshot held two identical
    healthy rows for that binding and lane_health() refused it as "unknown"
    forever, on the same snapshot whose glm and escalate rows admitted fine.
    The gateway's own /health said kimi was up the whole time. A fail-closed
    gate refused positive evidence solely because it was written down twice.
    """
    lanes = [
        {"name": "qwen", "model": "qwen-model"},
        {"name": "kimi", "model": "kimi-for-coding"},
        {"name": "kimi", "model": "kimi-for-coding"},
        {"name": "kimi", "model": "k3"},
    ]
    domains = {"qwen": ("qwen-a", "qwen-b"), "kimi": ("codex",)}
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        lanes,
        domains,
        tmp_path / "lane-health.json",
        "cycle-1",
        opener=_opener(_documents(), []),
        revision_resolver=lambda endpoint: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    kimi_rows = [(row["lane"], row["model"]) for row in snapshot["lanes"] if row["lane"] == "kimi"]
    assert kimi_rows == [("kimi", "kimi-for-coding"), ("kimi", "k3")]
    assert lane_health(
        snapshot,
        "kimi",
        "kimi-for-coding",
        cycle_id="cycle-1",
        endpoint=ENDPOINT,
        capacity_domains=("codex",),
        active_revision=REVISION,
        now=2_000_000_001.0,
    ) == (True, "healthy")


def test_identical_duplicate_rows_in_a_sealed_snapshot_still_admit(tmp_path: Path) -> None:
    """lane_health itself collapses byte-identical duplicates.

    A deployed rotator that still emits the duplicate must recover on a library
    upgrade alone, without a same-day script redeploy: two identical rows are
    the same observation stated twice, not conflicting evidence.
    """
    snapshot, _, _ = _acquire(tmp_path, _documents())
    duplicated = dict(snapshot)
    qwen_row = next(row for row in snapshot["lanes"] if row["lane"] == "qwen")
    duplicated["lanes"] = [*snapshot["lanes"], json.loads(json.dumps(qwen_row))]
    assert _admit(duplicated, "qwen", "qwen-model") == (True, "healthy")


def test_conflicting_duplicate_rows_still_fail_closed(tmp_path: Path) -> None:
    """Two rows for one binding that DISAGREE stay refused: that is ambiguity."""
    snapshot, _, _ = _acquire(tmp_path, _documents())
    forged = dict(snapshot)
    qwen_row = next(row for row in snapshot["lanes"] if row["lane"] == "qwen")
    altered = json.loads(json.dumps(qwen_row))
    altered["domains"] = [
        {"capacity_domain": "qwen-a", "state": "owner-down"},
        {"capacity_domain": "qwen-b", "state": "owner-down"},
    ]
    forged["lanes"] = [*snapshot["lanes"], altered]
    assert _admit(forged, "qwen", "qwen-model") == (False, "unknown")


def test_rotator_health_lanes_carry_no_duplicate_bindings() -> None:
    """The rotator's generated health-lane list is duplicate-free, kimi included."""
    script = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text(encoding="utf-8"))
    start = next(
        i
        for i, node in enumerate(tree.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_health_lanes" for t in node.targets)
    )
    end = next(i for i in range(start + 1, len(tree.body)) if isinstance(tree.body[i], ast.Assign))
    namespace = {
        "LANES": [
            {"name": "codex", "model": "sk-codex-mid"},
            {"name": "glm", "model": "sk-glm-s"},
            {"name": "qwen", "model": "qwen-model"},
            {"name": "kimi", "model": "kimi-for-coding"},
            {"name": "escalate", "model": "gpt-strong"},
        ],
        "_GLM_LEVELS": {"S": "sk-glm-s", "M": "sk-glm-m", "L": "sk-glm-l", "XL": "sk-glm-l"},
        "_SIZE_MODELS": {"S": "sk-s", "M": "sk-m", "L": "sk-l", "XL": "sk-xl"},
    }
    exec(
        compile(ast.Module(body=tree.body[start:end], type_ignores=[]), str(script), "exec"),
        namespace,
    )
    bindings = [(lane["name"], lane["model"]) for lane in namespace["_health_lanes"]]
    assert len(bindings) == len(set(bindings)), bindings
    assert ("kimi", "kimi-for-coding") in bindings
    assert ("kimi", "k3") in bindings
