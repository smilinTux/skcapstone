"""Real native claims fence stale reservation and process creation."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from skcoord.card_store import CardCore, CardStore, card_mutation_lock

from skcapstone.fleet import production_admission as admission
from tests.fleet.test_production_admission import BINDING, HOST, POLICY, UNIT, command


@pytest.fixture
def native(tmp_path, monkeypatch):
    (tmp_path / "fleet").mkdir()
    monkeypatch.setattr(admission.socket, "gethostname", lambda: HOST)
    monkeypatch.setattr(admission, "active_resource_units", lambda home: [])
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "fixture"))
    store = CardStore(tmp_path)
    store.create(CardCore(id=BINDING["card_id"], title="Synthetic admission fixture",
                          initial_owner=BINDING["owner"],
                          initial_claim_revision=BINDING["claim_revision"]))
    return tmp_path, store


def reserve(native, binding=BINDING):
    return admission.reserve_launch(native[0], POLICY, HOST, UNIT, binding, command())


def release(native):
    home, store = native
    with card_mutation_lock(home, BINDING["card_id"]):
        store.append_event(BINDING["card_id"], "release_claim", BINDING["owner"],
                           released_owner=BINDING["owner"],
                           expected_claim_revision=BINDING["claim_revision"])


def test_released_claim_cannot_reserve(native):
    release(native)
    with pytest.raises(admission.AdmissionError, match="claim changed"):
        reserve(native)
    assert not list(native[0].glob("fleet/resource-admission/*/*/intent.json"))


def test_release_between_reservation_and_start_refuses_spawn(native):
    argv = reserve(native)
    release(native)
    called = []
    with pytest.raises(admission.AdmissionError, match="claim changed"):
        admission.start_reserved(native[0], HOST, argv, called.append)
    assert called == []
    assert not list(native[0].glob("fleet/resource-admission/*/*/start.json"))


def test_same_owner_new_revision_is_not_old_generation(native):
    argv = reserve(native)
    release(native)
    native[1].append_event(BINDING["card_id"], "claim", BINDING["owner"],
                           owner=BINDING["owner"], claim_revision="replacement")
    with pytest.raises(admission.AdmissionError, match="claim changed"):
        admission.start_reserved(native[0], HOST, argv, lambda _: pytest.fail("spawned"))


def test_start_holds_real_claim_lock_until_spawn_returns(native):
    argv = reserve(native)
    entered, finish, released = threading.Event(), threading.Event(), threading.Event()

    def spawn(command):
        entered.set()
        assert finish.wait(3)
        assert native[1].fold(BINDING["card_id"]).owner == BINDING["owner"]
        return 17

    def release_other_thread():
        release(native)
        released.set()

    with ThreadPoolExecutor(2) as pool:
        start = pool.submit(admission.start_reserved, native[0], HOST, argv, spawn)
        assert entered.wait(3)
        mutation = pool.submit(release_other_thread)
        assert not released.wait(.1)
        finish.set()
        assert start.result(3) == 17
        mutation.result(3)
    assert released.is_set()


def test_operator_binding_shape_preserved_and_start_is_not_replayable(native):
    argv = reserve(native, dict(BINDING, operation="synthetic-operator-renewal",
                               source_head="a" * 40))
    assert [argv[0], *argv[2:]] == command()
    called = []
    admission.start_reserved(native[0], HOST, argv, called.append)
    with pytest.raises(admission.AdmissionError, match="custody unavailable"):
        admission.start_reserved(native[0], HOST, argv, called.append)
    assert len(called) == 1


def test_unknown_spawn_retains_consumed_reservation(native):
    argv = reserve(native)
    def fail(_):
        raise OSError("lost acknowledgement")
    with pytest.raises(admission.AdmissionError):
        admission.start_reserved(native[0], HOST, argv, fail)
    assert len(list(native[0].glob("fleet/resource-admission/*/*/start.json"))) == 1
    assert native[1].fold(BINDING["card_id"]).owner == BINDING["owner"]
