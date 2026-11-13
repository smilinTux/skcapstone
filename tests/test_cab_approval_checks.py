"""Every check in ``skcapstone.cab_approval``, one fault at a time (card bada5c86).

Each case is valid in every respect but one, so the check under test is the only
thing that can refuse it. This is what kills the mutants that disable a single
comparison: the signature, the pinned identity, each part of the auto-approval
shape, and the rest.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from skcapstone import cab_approval as ca
from skcapstone.operator_authorization import AuthorizationEnvelope, authorization_id
from tests.cab_grants import PASSPHRASE, key

CHG = "chg-checks"


def _pin(name="chef", roles=("owner", "approver"), armor_of=None):
    return {
        key(name).fingerprint.upper(): {
            "identity": name,
            "roles": frozenset(roles),
            "backend": "pgpy",
            "public_key": key(armor_of or name).public_armor,
        }
    }


def _grant(signer="chef", **over):
    from capauth.crypto import get_backend

    fields = dict(
        authorization_id="pending",
        issuer="chef",
        issuer_role="owner",
        issuer_fingerprint=key("chef").fingerprint,
        action="itil.cab.vote.approved",
        target="t",
        change_id=CHG,
        scope="s",
        issued_at="2026-11-13T00:00:00+00:00",
        expires_at="2026-11-13T00:10:00+00:00",
        nonce="n" * 32,
    )
    fields.update(over)
    env = AuthorizationEnvelope(**fields)
    env.authorization_id = authorization_id(env)
    env.signature = get_backend().sign(env.signing_bytes(), key(signer).private_armor, PASSPHRASE)
    return env.model_dump()


def _vote_for(env, **over):
    fields = dict(
        change_id=env["change_id"],
        agent=env["issuer"],
        decision="approved",
        decided_at="2026-11-13T00:00:01+00:00",
        subject_role=env["issuer_role"],
        subject_fingerprint=env["issuer_fingerprint"],
        authorization_id=env["authorization_id"],
        authorization=env,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


def test_baseline_verifies():
    env = _grant()
    assert ca.verify_vote_envelope(_vote_for(env), env, _pin())


def test_the_signature_must_verify():
    """S1: a grant claiming Chef's fingerprint but signed by another key."""
    env = _grant(signer="mallory")
    assert not ca.verify_vote_envelope(_vote_for(env), env, _pin())


def test_the_key_must_have_the_pinned_fingerprint():
    env = _grant(signer="mallory")
    assert not ca.verify_vote_envelope(_vote_for(env), env, _pin(armor_of="mallory"))
    fpr = key("chef").fingerprint.upper()
    body = AuthorizationEnvelope.model_validate(env).signing_bytes()
    assert not ca.signature_ok("pgpy", fpr, body, env["signature"], key("mallory").public_armor)


def test_pinned_identity_must_be_the_issuer():
    """S3: Chef's key signing a grant that names lumina does not let lumina approve."""
    env = _grant(issuer="lumina")
    assert not ca.verify_vote_envelope(_vote_for(env), env, _pin("chef"))


def test_role_must_be_one_the_pin_grants():
    env = _grant(issuer_role="owner")
    assert not ca.verify_vote_envelope(_vote_for(env), env, _pin(roles=("approver",)))


def test_unpinned_fingerprint_is_refused():
    env = _grant()
    assert not ca.verify_vote_envelope(_vote_for(env), env, {})


@pytest.mark.parametrize(
    "over",
    [
        {"schema_version": "atlas-authorization/v2"},
        {"action": "itil.cab.vote.rejected"},
        {"issuer_role": "operator"},
        {"nonce": "short"},
    ],
    ids=["schema", "action", "non-human-role", "nonce"],
)
def test_each_envelope_rule(over):
    env = _grant(**over)
    pin = _pin(roles=("owner", "approver", "operator"))
    assert not ca.verify_vote_envelope(_vote_for(env), env, pin)


def test_empty_signature_and_non_content_id_are_refused():
    env = _grant()
    assert not ca.verify_vote_envelope(_vote_for(env), {**env, "signature": ""}, _pin())
    bad = {**env, "authorization_id": "authz-" + "0" * 24}
    assert not ca.verify_vote_envelope(_vote_for(bad), bad, _pin())


@pytest.mark.parametrize(
    "over",
    [
        {"change_id": "chg-elsewhere"},
        {"subject_role": "approver"},
        {"subject_fingerprint": "B" * 40},
        {"authorization_id": "authz-" + "1" * 24},
        {"agent": "lumina"},
    ],
    ids=["change", "role", "fingerprint", "authorization-id", "voter"],
)
def test_the_vote_must_match_its_grant(over):
    env = _grant()
    assert not ca.verify_vote_envelope(_vote_for(env, **over), env, _pin())


def test_field_set_is_exact_and_non_envelopes_fail():
    env = _grant()
    assert not ca.verify_vote_envelope(_vote_for(env), {**env, "extra": "x"}, _pin())
    assert not ca.verify_vote_envelope(_vote_for(env), None, _pin())
    assert not ca.verify_vote_envelope(_vote_for(env), {"schema_version": 1}, _pin())


@pytest.fixture()
def signature_always_ok(monkeypatch):
    monkeypatch.setattr(ca, "signature_ok", lambda *a: True)


def test_structural_checks_hold_without_the_signature(signature_always_ok):
    lumina = _grant(issuer="lumina")
    assert not ca.verify_vote_envelope(_vote_for(lumina), lumina, _pin("chef"))
    env = _grant()
    assert ca.verify_vote_envelope(_vote_for(env), env, _pin())
    assert not ca.verify_vote_envelope(_vote_for(env), {**env, "signature": ""}, _pin())


def test_an_auto_approved_change_reports_no_human_approval(monkeypatch):
    """A human vote on a standard or auto-normal change does not make it human-approved."""
    monkeypatch.setattr(ca, "load_approvers", lambda path=None: _pin())
    vote = _vote_for(_grant())
    assert ca.human_approval_times(_ITIL([vote]), _change())
    assert ca.human_approval_times(_ITIL([vote]), _change(**AUTO)) == []
    standard = _change(change_type=SimpleNamespace(value="standard"))
    assert ca.human_approval_times(_ITIL([vote]), standard) == []


# ── qualifying_human_approval ──────────────────────────────────────────────


def _change(**over):
    fields = dict(
        id=CHG,
        change_type=SimpleNamespace(value="normal"),
        risk=SimpleNamespace(value="low"),
        status=SimpleNamespace(value="approved"),
        tags=[],
        created_by="atlas",
        managed_by="atlas",
        prepared_by=None,
        rollback_plan="revert",
    )
    fields.update(over)
    return SimpleNamespace(**fields)


class _ITIL:
    cab_dir = "/nonexistent"

    def __init__(self, votes):
        self.votes = votes

    def get_cab_votes(self, _change_id):
        return self.votes


def test_a_valid_grant_qualifies(monkeypatch):
    env = _grant()
    vote = _vote_for(env)
    assert ca.qualifying_human_approval(_ITIL([vote]), vote, _change(), _pin())


@pytest.mark.parametrize("field", ["prepared_by", "created_by", "managed_by"])
def test_drafter_creator_and_owner_never_qualify(field):
    env = _grant()
    vote = _vote_for(env)
    change = _change(**{field: "capauth:chef@skworld.io"})
    assert not ca.qualifying_human_approval(_ITIL([vote]), vote, change, _pin())


@pytest.mark.parametrize("voter", ["human", "operator", "atlas", "device:ab12", "operator:cd"])
def test_machine_and_placeholder_voters_never_qualify(voter):
    env = _grant(issuer=voter)
    vote = _vote_for(env)
    assert not ca.qualifying_human_approval(_ITIL([vote]), vote, _change(), _pin(voter))


def test_only_approvals_qualify():
    env = _grant(action="itil.cab.vote.rejected")
    vote = _vote_for(env, decision="rejected")
    assert not ca.qualifying_human_approval(_ITIL([vote]), vote, _change(), _pin())


# ── auto_approved: each condition alone ─────────────────────────────────────

AUTO = dict(
    change_type=SimpleNamespace(value="normal"),
    tags=["auto-normal"],
    created_by="operator",
    risk=SimpleNamespace(value="low"),
    rollback_plan="revert",
)


def test_auto_normal_shape_is_auto_approved():
    assert ca.auto_approved(_change(**AUTO), [])


def test_standard_is_auto_approved_whatever_else():
    assert ca.auto_approved(_change(change_type=SimpleNamespace(value="standard")), [])


@pytest.mark.parametrize(
    "over",
    [
        {"created_by": "atlas"},  # S4: the creator check alone
        {"tags": ["operator"]},  # S5: the tag check alone
        {"risk": SimpleNamespace(value="high")},
        {"rollback_plan": "   "},
        {"change_type": SimpleNamespace(value="emergency")},
    ],
    ids=["creator", "tag", "risk", "rollback", "type"],
)
def test_each_auto_normal_condition_alone(over):
    assert not ca.auto_approved(_change(**{**AUTO, **over}), [])


def test_a_named_rejection_ends_auto_normal_but_a_placeholder_does_not():
    named = SimpleNamespace(agent="sentinel", decision=SimpleNamespace(value="rejected"))
    anon = SimpleNamespace(agent="human", decision=SimpleNamespace(value="rejected"))
    assert not ca.auto_approved(_change(**AUTO), [named])
    assert ca.auto_approved(_change(**AUTO), [anon])


# ── human_approval_times / approval_basis ───────────────────────────────────


def test_approval_time_is_the_signed_issued_at(monkeypatch):
    """Not decided_at, which anyone able to write the vote file can re-date."""
    env = _grant(issued_at="2026-11-01T00:00:00+00:00", expires_at="2026-11-01T00:10:00+00:00")
    vote = _vote_for(env, decided_at="2026-11-13T00:00:00+00:00")
    monkeypatch.setattr(ca, "load_approvers", lambda path=None: _pin())
    assert ca.human_approval_times(_ITIL([vote]), _change()) == ["2026-11-01T00:00:00+00:00"]


def test_a_named_rejection_means_no_human_approval(monkeypatch):
    env = _grant()
    monkeypatch.setattr(ca, "load_approvers", lambda path=None: _pin())
    reject = SimpleNamespace(agent="sentinel", decision=SimpleNamespace(value="rejected"))
    assert ca.human_approval_times(_ITIL([_vote_for(env)]), _change())
    assert ca.human_approval_times(_ITIL([_vote_for(env), reject]), _change()) == []


def test_unreadable_votes_are_no_approval():
    class Broken:
        def get_cab_votes(self, _):
            raise OSError("gone")

    assert ca.human_approval_times(Broken(), _change()) == []
    assert ca.approval_basis(Broken(), _change()) is None


@pytest.mark.parametrize("status", ["proposed", "reviewing", "rejected", "closed"])
def test_approval_basis_needs_an_approved_or_scheduled_fold(status):
    change = _change(**AUTO, status=SimpleNamespace(value=status))
    assert ca.approval_basis(_ITIL([]), change) is None


@pytest.mark.parametrize("status", ["approved", "scheduled"])
def test_approval_basis_auto(status):
    change = _change(**AUTO, status=SimpleNamespace(value=status))
    assert ca.approval_basis(_ITIL([]), change) == "auto"


def test_approval_basis_human_and_none(monkeypatch):
    monkeypatch.setattr(ca, "load_approvers", lambda path=None: _pin())
    env = _grant()
    assert ca.approval_basis(_ITIL([_vote_for(env)]), _change()) == "human"
    assert ca.approval_basis(_ITIL([]), _change()) is None


# ── pin loading and stored envelopes ────────────────────────────────────────


def test_load_approvers_reads_key_files_and_normalises(tmp_path):
    armor = tmp_path / "public.asc"
    armor.write_text(key("chef").public_armor)
    fpr = key("chef").fingerprint
    spaced = " ".join(fpr[i : i + 4] for i in range(0, len(fpr), 4)).lower()
    path = tmp_path / "pin.json"
    path.write_text(
        json.dumps(
            {
                "approvers": [
                    {"identity": "chef", "fingerprint": spaced, "public_key_file": str(armor)},
                    {"identity": "noarmor", "fingerprint": "C" * 40},
                    {"fingerprint": key("lumina").fingerprint, "public_key": "x"},
                ]
            }
        )
    )
    approvers = ca.load_approvers(path)
    assert set(approvers) == {fpr.upper()}
    assert approvers[fpr.upper()]["roles"] == frozenset({"owner", "approver"})
    assert ca.pinned_approver(spaced, path)["identity"] == "chef"
    assert ca.load_approvers(tmp_path / "absent.json") == {}


def test_stored_envelope_reads_the_raw_vote_file(tmp_path):
    env = _grant()
    (tmp_path / f"{CHG}-chef.json").write_text(json.dumps({"authorization": env}))
    vote = SimpleNamespace(change_id=CHG, agent="chef", authorization=None)
    assert ca.stored_envelope(SimpleNamespace(cab_dir=tmp_path), vote) == env
    assert ca.stored_envelope(SimpleNamespace(cab_dir=tmp_path / "x"), vote) is None


def test_verify_grant_for_binds_change_and_decision():
    env = _grant()
    assert ca.verify_grant_for(env, change_id=CHG, decision="approved", approvers=_pin())
    assert not ca.verify_grant_for(env, change_id="chg-x", decision="approved", approvers=_pin())
    assert not ca.verify_grant_for(env, change_id=CHG, decision="rejected", approvers=_pin())
    assert not ca.verify_grant_for({}, change_id=CHG, decision="approved", approvers=_pin())


def test_is_machine_identity():
    for who in ("operator", "capauth:atlas@skworld.io", "device:ab", "operator:cd", "", "human"):
        assert ca.is_machine_identity(who)
    assert not ca.is_machine_identity("chef")
