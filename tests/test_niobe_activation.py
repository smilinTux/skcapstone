"""Activation gate contract, parameterised over the estate that owns it.

Every guarantee the chi-pinned version asserted is asserted here too, just
against an estate supplied by a fixture instead of against literals baked into
the module. Two estates are exercised so a second one is proven to work, and
a cross-estate case proves host A's activation cannot authorize host B.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.niobe_activation import ActivationError, parse_activation

NOW = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)

CHI = {"operator": "casey", "realm": "skworld.io", "host": "chiap08"}
NOR = {"operator": "chef", "realm": "skworld.io", "host": "noroc2027"}
SCOPE = ["skcapstone", "skdashboard", "skworld"]


def make_estate(home: Path, estate: dict, *, card_id: str = "c4e7a9b2") -> str:
    """Write one estate's synced authority record and approval card.

    Args:
        home: Estate home to populate.
        estate: One of the estate dicts above.
        card_id: The approval card the activation will reference.

    Returns:
        The card's current sha256 revision.
    """
    config = home / "config/estate.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "schema": "sk.estate-authority/v1",
                "operator": estate["operator"],
                "realm": estate["realm"],
                "product_scope": SCOPE,
            }
        )
    )
    core = home / "cards" / card_id / "core.json"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text(json.dumps({"id": card_id}) + "\n")
    return hashlib.sha256(core.read_bytes()).hexdigest()


def record(estate: dict, revision: str, **overrides):
    """Build a well-formed activation for the given estate."""
    value = {
        "schema": "skfleet.niobe-activation/v1",
        "state": "active",
        "seat": "niobe",
        "decision_id": f"{estate['operator']}-niobe-20260906",
        "authorized_by": estate["operator"],
        "card_id": "c4e7a9b2",
        "card_revision": revision,
        "host": estate["host"],
        "live_unit": "skfleet-niobe-live.timer",
        "product_scope": list(SCOPE),
        "card_label": "seat-niobe",
        "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
        "denied_actions": ["merge", "deploy", "application_actuation", "external_dispatch"],
        "expires_at": "2026-09-07T20:00:00+00:00",
        "rollback": {
            "owner": estate["operator"],
            "action": "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer",
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("estate", [CHI, NOR], ids=["chi", "nor"])
def test_exact_operator_scope_is_accepted_on_either_estate(tmp_path: Path, estate: dict) -> None:
    revision = make_estate(tmp_path, estate)
    activation = parse_activation(
        record(estate, revision), home=tmp_path, host=estate["host"], now=NOW
    )
    assert activation.allowed_actions == {"claim", "release", "launch", "stop", "reassign"}
    assert activation.host == estate["host"]


def test_second_estate_is_rejected_by_the_first_estates_operator(tmp_path: Path) -> None:
    revision = make_estate(tmp_path, NOR)
    with pytest.raises(ActivationError, match="requires chef authorization"):
        parse_activation(
            record(NOR, revision, authorized_by="casey"),
            home=tmp_path,
            host=NOR["host"],
            now=NOW,
        )


def test_cross_estate_activation_does_not_authorize_another_host(tmp_path: Path) -> None:
    """Host A's activation, replayed verbatim on host B, must be refused."""
    revision = make_estate(tmp_path, NOR)
    chi_record = record(CHI, revision)
    chi_record["authorized_by"] = NOR["operator"]
    with pytest.raises(ActivationError, match="this machine is noroc2027"):
        parse_activation(chi_record, home=tmp_path, host=NOR["host"], now=NOW)


def test_estate_without_an_authority_record_authorizes_nobody(tmp_path: Path, monkeypatch) -> None:
    core = tmp_path / "cards/c4e7a9b2/core.json"
    core.parent.mkdir(parents=True)
    core.write_text('{"id":"c4e7a9b2"}\n')
    revision = hashlib.sha256(core.read_bytes()).hexdigest()
    monkeypatch.setattr("skcapstone.estate.SYSTEM_CLUSTER_PATH", tmp_path / "absent.json")
    with pytest.raises(ActivationError, match="estate cannot authorize"):
        parse_activation(record(NOR, revision), home=tmp_path, host=NOR["host"], now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "chiap01"),
        ("authorized_by", "jarvis"),
        ("state", "pending"),
        ("denied_actions", ["merge"]),
        ("product_scope", ["skcapstone"]),
    ],
)
def test_scope_or_authority_mismatch_is_rejected(tmp_path: Path, field: str, value) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError):
        parse_activation(
            record(CHI, revision, **{field: value}),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )


def test_expired_activation_is_rejected(tmp_path: Path) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError, match="expired"):
        parse_activation(
            record(CHI, revision, expires_at="2026-09-06T20:00:00+00:00"),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )


def test_extra_live_action_is_rejected(tmp_path: Path) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError, match="bounded"):
        parse_activation(
            record(
                CHI,
                revision,
                allowed_actions=["claim", "release", "launch", "stop", "reassign", "rotate"],
            ),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )


def test_missing_rollback_is_rejected(tmp_path: Path) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError, match="rollback"):
        parse_activation(
            record(CHI, revision, rollback=None), home=tmp_path, host=CHI["host"], now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("card_id", "wrong"),
        ("card_id", "../../etc"),
        ("card_revision", "short"),
        ("live_unit", "skfleet-rotate.timer"),
        ("card_revision", "z" * 64),
    ],
)
def test_activation_card_and_unit_fence_is_exact(tmp_path: Path, field: str, value: str) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError):
        parse_activation(
            record(CHI, revision, **{field: value}),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )


def test_well_formed_but_stale_card_revision_is_rejected(tmp_path: Path) -> None:
    """A revision that is valid sha256 but no longer the card's content fails."""
    make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError, match="revision is stale"):
        parse_activation(record(CHI, "b" * 64), home=tmp_path, host=CHI["host"], now=NOW)


def test_activation_referencing_an_absent_card_is_rejected(tmp_path: Path) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError, match="card is missing"):
        parse_activation(
            record(CHI, revision, card_id="deadbeef"),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )


def test_a_truthful_rollback_naming_another_fallback_is_accepted(tmp_path: Path) -> None:
    """The nor estate rolls back to its own bounded timer, not to a shadow unit."""
    revision = make_estate(tmp_path, NOR)
    activation = parse_activation(
        record(
            NOR,
            revision,
            rollback={
                "owner": "chef",
                "action": (
                    "disable_skfleet-niobe-live.timer; the bounded "
                    "skfleet-niobe.timer keeps running"
                ),
            },
        ),
        home=tmp_path,
        host=NOR["host"],
        now=NOW,
    )
    assert activation.rollback_owner == "chef"


def test_the_chi_rollback_literal_still_validates_unchanged(tmp_path: Path) -> None:
    """Records already on disk keep working: no migration, no silent invalidation."""
    revision = make_estate(tmp_path, CHI)
    activation = parse_activation(record(CHI, revision), home=tmp_path, host=CHI["host"], now=NOW)
    assert activation.rollback_action.startswith("disable_skfleet-niobe-live.timer")


@pytest.mark.parametrize(
    "action",
    ["", "   ", "notify_casey", "disable_skfleet-mero.timer"],
)
def test_a_rollback_that_does_not_disable_the_live_unit_is_rejected(
    tmp_path: Path, action: str
) -> None:
    revision = make_estate(tmp_path, CHI)
    with pytest.raises(ActivationError):
        parse_activation(
            record(CHI, revision, rollback={"owner": "casey", "action": action}),
            home=tmp_path,
            host=CHI["host"],
            now=NOW,
        )
