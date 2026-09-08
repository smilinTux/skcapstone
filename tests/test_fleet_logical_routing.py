"""Public synthetic logical admission and deployment replacement controls."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from skcapstone.fleet_lane_health import acquire_lane_snapshot, lane_health
from skcapstone.fleet_routing import BUCKETS, deployment_profile, logical_contract
from tests.test_fleet_lane_health import ENDPOINT, REVISION, _documents, _opener
from tests.test_skfleet_glm_levels import _rotate_namespace
from tests.test_skfleet_lane_affinity import _load_lane_helpers


@pytest.mark.parametrize("bucket", BUCKETS)
def test_logical_labels_and_titles_have_the_same_contract(bucket: str) -> None:
    """Provider labels are unnecessary, and unlabelled work defaults to medium."""
    lane = _load_lane_helpers()["lane_compatibility"]
    expected = ((bucket,), "logical:" + bucket)
    assert lane([bucket]) == expected
    assert lane([], title="[SKLEGAL][S1-05B][%s] Synthetic" % bucket[3:].upper()) == expected
    assert lane([]) == (("sk-m",), "logical:sk-m")
    assert logical_contract(["sk-m", "sk-l"])[0] == ()
    assert logical_contract(["sk-m"], "[XL] Synthetic")[0] == ()


@pytest.mark.parametrize("label", ["qwen-only", "sovereign", "qwen-sovereign"])
def test_explicit_sovereign_contract_has_no_frontier_fallback(label: str) -> None:
    """Explicit sovereign work remains pinned even if a frontier lane is healthy."""
    helpers = _load_lane_helpers()
    assert helpers["lane_compatibility"]([label]) == (("qwen",), "required-lane:qwen")
    assert helpers["select_compatible_lane"](
        [label],
        False,
        ["qwen", "codex"],
        {"qwen": 1, "codex": 1},
        lane_health_by_name={"codex": (True, "healthy")},
    ) == (None, "no-compatible-healthy-lane:qwen")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "null",
        "[]",
        "{",
        '{"version":true,"buckets":{}}',
        json.dumps({"version": 1, "buckets": {"sk-m": ["glm"]}}),
        *[
            json.dumps({"version": 1, "buckets": dict.fromkeys(BUCKETS, value)})
            for value in (["qwen"], ["unknown"], ["glm", "glm"], "glm", [None])
        ],
    ],
)
def test_invalid_deployment_profile_cannot_fall_back(raw: str) -> None:
    """Malformed or Qwen-contaminated profiles close ordinary admission."""
    profile = deployment_profile(raw)
    assert profile == {}
    select = _load_lane_helpers()["select_compatible_lane"]
    assert select([], False, ["glm"], {"glm": 1}, profile=profile)[0] is None


def test_profile_preference_capacity_and_absent_health_are_enforced() -> None:
    """Profile order wins; no free healthy route means no selection."""
    profile = deployment_profile(
        json.dumps(
            {
                "version": 1,
                "buckets": dict.fromkeys(BUCKETS, ["kimi", "glm"]),
            }
        )
    )
    helpers = _load_lane_helpers()
    select = helpers["select_compatible_lane"]
    remaining = {"kimi": 1, "glm": 1, "qwen": 9}
    assert select([], False, ["qwen", "glm", "kimi"], remaining, profile=profile)[0] == "kimi"
    assert (
        select([], False, ["kimi", "glm"], remaining, profile=profile, lane_health_by_name=None)[0]
        is None
    )
    assert select([], False, ["kimi", "glm"], {"kimi": 0, "glm": 1}, profile=profile)[0] == "glm"
    assert remaining == {"kimi": 1, "glm": 1, "qwen": 9}
    assert helpers["resolved_lane_compatibility"]([], profile=profile)[0] == ("kimi", "glm")


@pytest.mark.parametrize("bucket", BUCKETS)
@pytest.mark.parametrize("lane", ["glm", "kimi", "codex"])
def test_folded_logical_size_drives_the_actual_requested_model(
    monkeypatch, bucket: str, lane: str
) -> None:
    """Health and dispatch consume the same model for labels and title markers."""
    for size in ("S", "M", "L", "XL"):
        monkeypatch.setenv("SKFLEET_GLM_MODEL_" + size, "glm-" + size)
        monkeypatch.setenv("SKFLEET_CODEX_MODEL_" + size, "codex-" + size)
    ns = _rotate_namespace(
        {
            "_GLM_LEVEL_DEFAULTS",
            "_GLM_LEVELS",
            "_GLM_SIZE_RE",
            "_glm_model_for",
            "_CODEX_LEVEL_DEFAULTS",
            "_CODEX_LEVELS",
            "_codex_model_for",
            "_KIMI_SIZE_RE",
            "_kimi_model_for",
            "_lane_model",
        }
    )
    configured = {"name": lane, "model": "incorrect-fallback"}
    expected = (
        ("k3" if bucket == "sk-xl" else "kimi-for-coding")
        if lane == "kimi"
        else (lane + "-" + bucket[3:].upper())
    )
    assert ns["_lane_model"](configured, {}, [bucket]) == expected
    assert ns["_lane_model"](configured, {"title": "[%s]" % bucket[3:].upper()}) == expected
    medium = "kimi-for-coding" if lane == "kimi" else lane + "-M"
    assert ns["_lane_model"](configured, {}) == medium


def test_preclaim_rebinding_rejects_a_size_change_or_conflicting_labels() -> None:
    """A selected lane cannot survive a changed logical requirement unchecked."""
    profile = deployment_profile(
        json.dumps(
            {
                "version": 1,
                "buckets": {
                    "sk-s": ["glm"],
                    "sk-m": ["kimi"],
                    "sk-l": ["codex"],
                    "sk-xl": ["escalate"],
                },
            }
        )
    )
    helpers = _load_lane_helpers()
    selected, _ = helpers["select_compatible_lane"](
        ["sk-m"],
        False,
        ["kimi", "codex"],
        {"kimi": 1, "codex": 1},
        profile=profile,
    )
    assert selected == "kimi"
    for fresh_labels in (["sk-l"], ["sk-m", "sk-l"]):
        compatible, _ = helpers["resolved_lane_compatibility"](fresh_labels, profile=profile)
        assert selected not in compatible


@pytest.mark.parametrize("bucket", BUCKETS)
@pytest.mark.parametrize(
    ("lane", "model", "domain"),
    [
        ("glm", "glm-fixture", "zai"),
        ("kimi", "kimi-for-coding", "kimi-coding"),
        ("escalate", "astra-fixture", "astra"),
        ("codex", "codex-fixture", "codex"),
    ],
)
@pytest.mark.parametrize(
    "state",
    [
        "healthy",
        "unknown",
        "stale",
        "malformed",
        "unreachable",
        "unauthorized",
        "unhealthy",
        "quarantined",
    ],
)
def test_qualified_backend_replacement_preserves_fail_closed_matrix(
    tmp_path: Path, bucket: str, lane: str, model: str, domain: str, state: str
) -> None:
    """Every bucket admits only exact healthy evidence on each deployment."""
    documents = _documents()
    documents["/health"]["backends"] = {
        domain: {
            "status": "up",
            "observed": True,
            "quarantined": False,
            "lastCheck": 2_000_000_000_000,
        }
    }
    documents["/queue"]["backends"] = {domain: {"capacityDomain": domain, "max": 4}}
    row = documents["/health"]["backends"][domain]
    if state == "unknown":
        row.update(status="unknown", observed=False)
    elif state == "malformed":
        row["lastCheck"] = "yesterday"
    elif state == "unhealthy":
        row["status"] = "down"
    elif state == "quarantined":
        row["quarantined"] = True
    elif state == "unreachable":
        documents["/health"] = OSError("synthetic unreachable")
    elif state == "unauthorized":
        documents["/health"] = HTTPError(ENDPOINT, 401, "synthetic denial", {}, None)
    lanes = [{"name": lane, "model": model}]
    snapshot = acquire_lane_snapshot(
        ENDPOINT,
        lanes,
        {lane: (domain,)},
        tmp_path / "health.json",
        "cycle-1",
        opener=_opener(documents, []),
        revision_resolver=lambda endpoint: REVISION,
        now=lambda: 2_000_000_000.0,
    )
    health = lane_health(
        snapshot,
        lane,
        model,
        cycle_id="cycle-1",
        endpoint=ENDPOINT,
        capacity_domains=(domain,),
        active_revision=REVISION,
        now=2_000_000_600.0 if state == "stale" else 2_000_000_001.0,
    )
    profile = deployment_profile(
        json.dumps(
            {
                "version": 1,
                "buckets": dict.fromkeys(BUCKETS, [lane]),
            }
        )
    )
    helpers = _load_lane_helpers()
    assert helpers["lane_compatibility"]([bucket]) == ((bucket,), "logical:" + bucket)
    assert helpers["resolved_lane_compatibility"]([bucket], profile=profile)[0] == (lane,)
    selected, _ = helpers["select_compatible_lane"](
        [bucket],
        False,
        lanes,
        {lane: 1},
        lane_health_by_name={lane: health},
        profile=profile,
    )
    assert selected == (lane if state == "healthy" else None)
