"""sknoded's operator-plane HTTP surface (P1, card 90b5b277).

Covers: the gate defaults OFF and never binds a wildcard; request signing is
pinned to the CLAIMED fingerprint's own key; capability checks go through
capauth's decide() and observe never implies act; the failure taxonomy stays
three distinct, never-healthy families; the observe tree is GET-only and
freeze-independent (byte-identical fleet tree, still answers while frozen);
a cli/seat lane disagreement renders Unknown(LaneConflict) rather than
picking a winner; the reserved act path never actuates; and sknoded.main_loop
only starts the surface when the gate is on and it is not a one-shot pass.

NOTE: These tests use real HTTP sockets and require the network stack.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.network

from skcapstone.fleet import operator_http as oh
from skcapstone.fleet import sknoded, store
from skcapstone.fleet.paths import FleetPaths

PWT = frozenset({"GradingBacklog"})


def _tmp_fleet(tmp_path, *, cli="sh -c x", conditions=("Ready",)):
    paths = FleetPaths(root=tmp_path / "fleet")
    writer = store.Writer(role="operator", node="cli", identity="test")
    store.write_spec(
        paths,
        "operatorapp",
        "appx",
        {"name": "appx", "cli": cli, "conditions": list(conditions)},
        writer=writer,
    )
    return paths


def _ok_run(status="True"):
    def run(argv, timeout):
        return 0, json.dumps({"conditions": [{"type": "Ready", "status": status}]}), ""

    return run


# ── gate + bind ──────────────────────────────────────────────────────────


def test_gate_defaults_off(monkeypatch):
    monkeypatch.delenv(oh.GATE_ENV, raising=False)
    assert oh.http_enabled() is False


def test_gate_truthy_variants(monkeypatch):
    for val in ("1", "true", "YES", "On"):
        monkeypatch.setenv(oh.GATE_ENV, val)
        assert oh.http_enabled() is True
    for val in ("0", "", "off", "no"):
        monkeypatch.setenv(oh.GATE_ENV, val)
        assert oh.http_enabled() is False


@pytest.mark.parametrize("bad", ["", "0.0.0.0", "::", "  ", None])
def test_resolve_bind_host_refuses_blank_and_wildcard(monkeypatch, bad):
    monkeypatch.delenv(oh.BIND_ENV, raising=False)
    assert oh.resolve_bind_host(bad) is None


def test_resolve_bind_host_accepts_a_concrete_address():
    assert oh.resolve_bind_host("100.64.0.5") == "100.64.0.5"


def test_resolve_bind_host_reads_the_env_when_no_explicit_value(monkeypatch):
    monkeypatch.setenv(oh.BIND_ENV, "100.64.0.9")
    assert oh.resolve_bind_host() == "100.64.0.9"
    monkeypatch.setenv(oh.BIND_ENV, "0.0.0.0")
    assert oh.resolve_bind_host() is None


# ── failure taxonomy: three distinct, never-healthy families ───────────────


def test_every_reason_maps_to_exactly_one_of_three_named_families():
    families = {oh.FAMILY_UNREACHABLE, oh.FAMILY_UNKNOWN, oh.FAMILY_UNAUTHORIZED}
    assert set(oh.REASON_FAMILY.values()) <= families
    assert "healthy" not in oh.REASON_FAMILY.values()
    assert "True" not in oh.REASON_FAMILY.values()
    # Every REASON_* constant this module defines is present in the mapping.
    reason_constants = {
        v for k, v in vars(oh).items() if k.startswith("REASON_") and isinstance(v, str)
    }
    assert reason_constants == set(oh.REASON_FAMILY)


def test_the_three_families_are_pairwise_distinct():
    assert len({oh.FAMILY_UNREACHABLE, oh.FAMILY_UNKNOWN, oh.FAMILY_UNAUTHORIZED}) == 3


# ── request signing: pinned to the CLAIMED fingerprint's own key ───────────


def _sign(data: bytes, secret: str = "key-a") -> str:
    return f"sig:{secret}:" + hashlib.sha256(data + secret.encode()).hexdigest()


def _verify_one(data: bytes, sig: str, armored_key: str) -> bool:
    return sig == _sign(data, armored_key)


def _roster():
    # fingerprint -> "armored key" (our fakes just use the secret as the key)
    return {"AAAA": "key-a", "BBBB": "key-b"}


def _headers(method, path, body, *, fpr="AAAA", secret="key-a", ts=None, nonce="n1"):
    ts = str(time.time()) if ts is None else ts
    data = oh.canonical_request_bytes(method, path, body, ts, nonce)
    return {
        "X-SK-Fingerprint": fpr,
        "X-SK-Timestamp": ts,
        "X-SK-Nonce": nonce,
        "X-SK-Signature": _sign(data, secret),
    }


def test_verify_signed_request_happy_path():
    body = b""
    headers = _headers("GET", "/operator/v1/apps", body)
    result = oh.verify_signed_request(
        "GET", "/operator/v1/apps", body, headers, roster_by_fpr=_roster, verify_one=_verify_one
    )
    assert result.fingerprint == "AAAA"
    assert result.reason is None


def test_verify_signed_request_missing_headers():
    result = oh.verify_signed_request("GET", "/x", b"", {}, roster_by_fpr=_roster)
    assert result.reason == oh.REASON_UNAUTHORIZED


def test_verify_signed_request_unknown_fingerprint():
    headers = _headers("GET", "/x", b"", fpr="ZZZZ", secret="key-a")
    result = oh.verify_signed_request(
        "GET", "/x", b"", headers, roster_by_fpr=_roster, verify_one=_verify_one
    )
    assert result.reason == oh.REASON_UNAUTHORIZED


def test_verify_signed_request_signature_made_by_a_different_trusted_key_is_rejected():
    """Key B's valid signature must NOT authenticate a request claiming fingerprint A.

    This is the exact gap "any roster key verifies" would create: a caller
    holds a real, trusted key (B) but claims to be a DIFFERENT trusted
    identity (A). Pinning verification to the claimed fingerprint's own key
    is what makes this fail instead of silently laundering B's signature
    into A's identity.
    """
    data = oh.canonical_request_bytes("GET", "/x", b"", "123", "n1")
    headers = {
        "X-SK-Fingerprint": "AAAA",  # claims A
        "X-SK-Timestamp": "123",
        "X-SK-Nonce": "n1",
        "X-SK-Signature": _sign(data, "key-b"),  # but signed with B
    }
    result = oh.verify_signed_request(
        "GET",
        "/x",
        b"",
        headers,
        roster_by_fpr=_roster,
        verify_one=_verify_one,
        now=123.0,
    )
    assert result.reason == oh.REASON_SIGNATURE_INVALID
    assert result.fingerprint is None


def test_verify_signed_request_timestamp_skew_refused():
    stale_ts = str(time.time() - oh.AUTH_SKEW_S - 10)
    headers = _headers("GET", "/x", b"", ts=stale_ts)
    result = oh.verify_signed_request(
        "GET", "/x", b"", headers, roster_by_fpr=_roster, verify_one=_verify_one
    )
    assert result.reason == oh.REASON_SIGNATURE_INVALID


def test_verify_signed_request_replayed_nonce_refused():
    headers = _headers("GET", "/x", b"", nonce="only-once")
    cache = oh.NonceCache()
    first = oh.verify_signed_request(
        "GET", "/x", b"", headers, roster_by_fpr=_roster, verify_one=_verify_one, nonce_cache=cache
    )
    second = oh.verify_signed_request(
        "GET", "/x", b"", headers, roster_by_fpr=_roster, verify_one=_verify_one, nonce_cache=cache
    )
    assert first.reason is None
    assert second.reason == oh.REASON_SIGNATURE_INVALID


def test_verify_signed_request_tampered_body_fails():
    headers = _headers("GET", "/x", b"original")
    result = oh.verify_signed_request(
        "GET",
        "/x",
        b"tampered",
        headers,
        roster_by_fpr=_roster,
        verify_one=_verify_one,
    )
    assert result.reason == oh.REASON_SIGNATURE_INVALID


# ── capability authorization: observe never implies act ────────────────────


class _FakeDecision:
    def __init__(self, allow, reason):
        self.allow = allow
        self.reason = reason


def test_authorize_allow_and_deny():
    def decide_allow(subject, capability, **kw):
        return _FakeDecision(True, "ok")

    def decide_deny(subject, capability, **kw):
        return _FakeDecision(False, "no grant")

    assert oh.authorize("AAAA", oh.SCOPE_OBSERVE, decide_fn=decide_allow) == (True, "ok")
    assert oh.authorize("AAAA", oh.SCOPE_ACT, decide_fn=decide_deny) == (False, "no grant")


def test_authorize_fails_closed_with_a_real_empty_capauth_store(tmp_path):
    """No capability tokens anywhere: decide() denies, never raises."""
    from capauth import decide as real_decide

    allow, reason = oh.authorize(
        "deadbeef", oh.SCOPE_OBSERVE, decide_fn=real_decide, base_dir=tmp_path / "empty"
    )
    assert allow is False
    assert reason


def test_observe_scope_never_implies_act_scope():
    """A subject granted ONLY operator.observe must be denied operator.act."""

    def decide_fn(subject, capability, **kw):
        return _FakeDecision(capability == oh.SCOPE_OBSERVE, "scoped grant")

    assert oh.authorize("AAAA", oh.SCOPE_OBSERVE, decide_fn=decide_fn)[0] is True
    assert oh.authorize("AAAA", oh.SCOPE_ACT, decide_fn=decide_fn)[0] is False


# ── envelope building: cli-first, seat fallback, lane conflict ─────────────


def test_collect_app_lanes_prefers_cli_when_ok(tmp_path):
    paths = _tmp_fleet(tmp_path)
    lanes = oh.collect_app_lanes(paths, run=_ok_run("True"), adapters={}, problem_when_true=PWT)
    entry = lanes["appx"]
    assert entry["cli_lane"]["state"] == "ok"
    envelope = oh.build_observation_envelope("appx", entry)
    assert envelope["conditions"][0]["provenance"] == "cli-local:appx"
    assert envelope["conditions"][0]["status"] == "True"


def test_build_envelope_falls_back_to_seat_when_cli_unavailable(tmp_path):
    paths = _tmp_fleet(tmp_path, cli="definitely-not-a-binary-xyz operator")

    def seat_ok(paths_, now_iso):
        return {"conditions": [{"type": "Ready", "status": "False"}]}

    lanes = oh.collect_app_lanes(paths, adapters={"appx": seat_ok}, problem_when_true=PWT)
    entry = lanes["appx"]
    assert entry["cli_lane"]["state"] == "no-cli"
    assert entry["seat_lane"]["state"] == "ok"
    envelope = oh.build_observation_envelope("appx", entry)
    assert envelope["conditions"][0]["provenance"] == "builtin:appx"
    assert envelope["conditions"][0]["status"] == "False"


def test_build_envelope_both_lanes_failing_is_unknown_never_healthy(tmp_path):
    paths = _tmp_fleet(tmp_path, cli="definitely-not-a-binary-xyz operator")
    lanes = oh.collect_app_lanes(paths, adapters={}, problem_when_true=PWT)
    entry = lanes["appx"]
    envelope = oh.build_observation_envelope("appx", entry)
    (cond,) = envelope["conditions"]
    assert cond["status"] == "Unknown"
    assert cond["reason"] == oh.REASON_NO_ENDPOINT
    assert oh.REASON_FAMILY[cond["reason"]] == oh.FAMILY_UNREACHABLE


def test_build_envelope_lane_conflict_is_unknown_not_a_silent_winner(tmp_path):
    paths = _tmp_fleet(tmp_path)

    def seat_disagrees(paths_, now_iso):
        return {"conditions": [{"type": "Ready", "status": "False"}]}

    lanes = oh.collect_app_lanes(
        paths, run=_ok_run("True"), adapters={"appx": seat_disagrees}, problem_when_true=PWT
    )
    entry = lanes["appx"]
    assert entry["cli_lane"]["state"] == "ok" and entry["seat_lane"]["state"] == "ok"
    assert entry["conflicts"], "the fixture must actually disagree"
    envelope = oh.build_observation_envelope("appx", entry)
    (cond,) = envelope["conditions"]
    assert cond["status"] == "Unknown"
    assert cond["reason"] == oh.REASON_LANE_CONFLICT
    assert oh.REASON_FAMILY[cond["reason"]] == oh.FAMILY_UNKNOWN


# ── freeze independence: GET-only, never gated, byte-identical tree ────────


def test_collect_app_lanes_never_touches_freeze_and_still_answers_while_frozen(tmp_path):
    paths = _tmp_fleet(tmp_path)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")
    frozen_bytes = paths.freeze_path().read_bytes()

    before = sorted(
        (p, p.stat().st_mtime_ns) for p in (tmp_path / "fleet").rglob("*") if p.is_file()
    )
    lanes = oh.collect_app_lanes(paths, run=_ok_run("True"), adapters={}, problem_when_true=PWT)
    after = sorted(
        (p, p.stat().st_mtime_ns) for p in (tmp_path / "fleet").rglob("*") if p.is_file()
    )

    assert before == after, "observe must never write to the fleet store, frozen or not"
    assert paths.freeze_path().read_bytes() == frozen_bytes
    # And it still produced a real answer -- no "standing down" branch exists here.
    assert lanes["appx"]["cli_lane"]["state"] == "ok"


def test_route_apps_and_observe_work_while_frozen(tmp_path):
    paths = _tmp_fleet(tmp_path)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")

    deps = _allow_all_deps(paths, run=_ok_run("True"))
    resp = oh.route("GET", "/operator/v1/apps/appx/observe", {}, b"", deps)
    assert resp.status == 200
    assert resp.body["conditions"][0]["status"] == "True"


# ── the pure router ──────────────────────────────────────────────────────


def _allow_all_deps(paths, node="node-test", *, run=None, adapters=None):
    """RouterDeps with auth stubbed to always allow, and lane collection
    deterministic by default (an injected fake ``run``, never a real
    subprocess) so tests never depend on what ``sh`` happens to do on the
    machine running them."""

    def verify_ok(method, path, body, headers):
        return oh.AuthResult("AAAA", None, "ok")

    def authorize_ok(fingerprint, capability, **kw):
        return True, "ok"

    return oh.RouterDeps(
        paths=paths,
        node=node,
        verify_request=verify_ok,
        authorize_fn=authorize_ok,
        run=run if run is not None else _ok_run("True"),
        adapters=adapters if adapters is not None else {},
        problem_when_true=PWT,
    )


def test_route_unknown_path_is_404():
    deps = oh.RouterDeps(paths=FleetPaths(root="/nonexistent"), node="n")
    resp = oh.route("GET", "/nope", {}, b"", deps)
    assert resp.status == 404


def test_route_healthz_and_readyz_need_no_auth(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    paths.objects.mkdir(parents=True)

    def verify_deny(method, path, body, headers):
        return oh.AuthResult(None, oh.REASON_UNAUTHORIZED, "no headers")

    deps = oh.RouterDeps(paths=paths, node="n", verify_request=verify_deny)
    assert oh.route("GET", "/operator/v1/healthz", {}, b"", deps).status == 200
    assert oh.route("GET", "/operator/v1/readyz", {}, b"", deps).status == 200


def test_route_readyz_503_when_registry_unreadable(tmp_path):
    paths = FleetPaths(root=tmp_path / "does-not-exist")
    deps = oh.RouterDeps(paths=paths, node="n")
    resp = oh.route("GET", "/operator/v1/readyz", {}, b"", deps)
    assert resp.status == 503
    assert resp.body["failing"]


def test_route_apps_requires_auth_then_lists_them(tmp_path):
    paths = _tmp_fleet(tmp_path)
    deps_noauth = oh.RouterDeps(paths=paths, node="n")
    resp = oh.route("GET", "/operator/v1/apps", {}, b"", deps_noauth)
    assert resp.status == 401
    assert resp.body["family"] == oh.FAMILY_UNAUTHORIZED

    deps = _allow_all_deps(paths)
    resp = oh.route("GET", "/operator/v1/apps", {}, b"", deps)
    assert resp.status == 200
    names = {a["name"] for a in resp.body["apps"]}
    assert "appx" in names


def test_route_apps_authorized_but_capability_missing_is_403():
    def verify_ok(method, path, body, headers):
        return oh.AuthResult("AAAA", None, "ok")

    def authorize_deny(fingerprint, capability, **kw):
        return False, "no grant"

    deps = oh.RouterDeps(
        paths=FleetPaths(root="/x"),
        node="n",
        verify_request=verify_ok,
        authorize_fn=authorize_deny,
    )
    resp = oh.route("GET", "/operator/v1/apps", {}, b"", deps)
    assert resp.status == 403
    assert resp.body["reason"] == oh.REASON_CAPABILITY_MISSING


def test_route_explain_known_and_unknown_app(tmp_path):
    paths = _tmp_fleet(tmp_path)
    deps = _allow_all_deps(paths)
    resp = oh.route("GET", "/operator/v1/apps/cmdb/explain", {}, b"", deps)
    assert resp.status == 200
    assert "conditions" in resp.body

    resp = oh.route("GET", "/operator/v1/apps/definitely-not-an-app/explain", {}, b"", deps)
    assert resp.status == 404
    assert resp.body["reason"] == oh.REASON_NO_ENDPOINT


def test_route_observe_unknown_app_is_404(tmp_path):
    paths = _tmp_fleet(tmp_path)
    deps = _allow_all_deps(paths)
    resp = oh.route("GET", "/operator/v1/apps/nope/observe", {}, b"", deps)
    assert resp.status == 404
    assert resp.body["reason"] == oh.REASON_NO_ENDPOINT


def test_route_estate_is_single_node_scoped(tmp_path):
    paths = _tmp_fleet(tmp_path)
    deps = _allow_all_deps(paths)
    resp = oh.route("GET", "/operator/v1/estate", {}, b"", deps)
    assert resp.status == 200
    assert resp.body["scope"] == "single-node"
    assert any(a["app"] == "appx" for a in resp.body["apps"])


def test_route_act_is_reserved_and_reports_freeze_but_never_actuates(tmp_path):
    paths = _tmp_fleet(tmp_path)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")

    deps = _allow_all_deps(paths)
    resp = oh.route("POST", "/operator/v1/apps/appx/act", {}, b"", deps)
    assert resp.status == 501
    assert resp.body["frozen"] is True

    # Unfrozen: still reserved/unimplemented, never actually 200s an actuation.
    store.set_frozen(paths, False, writer=human)
    resp = oh.route("POST", "/operator/v1/apps/appx/act", {}, b"", deps)
    assert resp.status == 501
    assert resp.body["frozen"] is False


def test_route_act_uses_the_act_scope_not_observe(tmp_path):
    paths = _tmp_fleet(tmp_path)
    seen = []

    def verify_ok(method, path, body, headers):
        return oh.AuthResult("AAAA", None, "ok")

    def authorize_recording(fingerprint, capability, **kw):
        seen.append(capability)
        return True, "ok"

    deps = oh.RouterDeps(
        paths=paths, node="n", verify_request=verify_ok, authorize_fn=authorize_recording
    )
    oh.route("POST", "/operator/v1/apps/appx/act", {}, b"", deps)
    assert seen == [oh.SCOPE_ACT]


# ── envelope signing ─────────────────────────────────────────────────────


def test_sign_envelope_without_a_signer_is_explicitly_none():
    out = oh.sign_envelope({"schema": "x", "app": "a", "conditions": []}, signer=None)
    assert out["signature"] is None
    assert out["signer_fpr"] is None


def test_sign_envelope_with_a_signer_populates_both_fields():
    def fake_signer(data: bytes) -> str:
        return "sig:" + hashlib.sha256(data).hexdigest()

    envelope = {"schema": "x", "app": "a", "conditions": [{"type": "Ready", "status": "True"}]}
    out = oh.sign_envelope(envelope, signer=fake_signer, signer_fpr="AAAA")
    assert out["signature"].startswith("sig:")
    assert out["signer_fpr"] == "AAAA"
    # The signature covers the envelope minus the signature slot itself.
    expected = fake_signer(oh._envelope_canonical_bytes(envelope))
    assert out["signature"] == expected


# ── watch cursor ─────────────────────────────────────────────────────────


def test_cursor_state_only_bumps_on_change():
    cs = oh.CursorState()
    c1 = cs.bump_if_changed("digest-a")
    c2 = cs.bump_if_changed("digest-a")
    c3 = cs.bump_if_changed("digest-b")
    assert c1 == c2
    assert c3 > c1
    assert cs.has_cursor(c1)
    assert cs.has_cursor(0)  # zero always means "no prior cursor"


def test_cursor_state_stale_cursor_is_not_held():
    cs = oh.CursorState()
    assert cs.has_cursor(999) is False


def test_watch_stream_sends_heartbeat_when_nothing_changes(tmp_path):
    paths = _tmp_fleet(tmp_path)
    deps = oh.RouterDeps(paths=paths, node="n")
    cs = oh.CursorState()
    stop = threading.Event()
    frames = []

    def write(chunk):
        frames.append(chunk)
        if len(frames) >= 2:
            stop.set()

    oh.watch_stream(
        deps,
        cs,
        client_cursor=0,
        write=write,
        stop_event=stop,
        poll_s=0.01,
        heartbeat_s=0.01,
    )
    # First frame is the initial observation (cursor bumps from 0), the rest heartbeats.
    assert frames[0].startswith(b"id: 1\ndata:")
    assert b"heartbeat" in frames[1]


# ── real socket: end-to-end auth + refusal + clean shutdown ────────────────


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_start_background_refuses_a_wildcard_host_even_when_explicit(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    handle = sknoded_start(paths, host="0.0.0.0")
    assert handle is None


def sknoded_start(paths, **kw):
    return oh.start_background(paths, "node-test", **kw)


def test_start_background_serves_healthz_unauth_and_refuses_apps_unauth(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    paths.objects.mkdir(parents=True)
    handle = sknoded_start(paths, host="127.0.0.1", port=0)
    assert handle is not None
    try:
        base = f"http://127.0.0.1:{handle.port}/operator/v1"
        with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["status"] == "ok"

        try:
            urllib.request.urlopen(f"{base}/apps", timeout=5)
            pytest.fail("expected HTTPError 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
            body = json.loads(exc.read())
            assert body["family"] == oh.FAMILY_UNAUTHORIZED
    finally:
        handle.stop()


# ── sknoded.main_loop wiring ─────────────────────────────────────────────


def test_main_loop_never_starts_the_surface_when_the_gate_is_off(paths, monkeypatch):
    monkeypatch.delenv(oh.GATE_ENV, raising=False)
    monkeypatch.setattr(
        oh, "start_background", lambda *a, **kw: pytest.fail("must not start: gate is off")
    )
    sknoded.main_loop(paths, "node-41", once=True)


def test_main_loop_never_starts_the_surface_for_a_one_shot_pass(paths, monkeypatch):
    monkeypatch.setenv(oh.GATE_ENV, "1")
    monkeypatch.setattr(
        oh, "start_background", lambda *a, **kw: pytest.fail("must not start: once=True")
    )
    sknoded.main_loop(paths, "node-41", once=True)


def test_main_loop_starts_the_surface_when_gated_on_and_daemon_mode(paths, monkeypatch):
    monkeypatch.setenv(oh.GATE_ENV, "1")
    calls = []
    monkeypatch.setattr(oh, "start_background", lambda p, n, **kw: calls.append((p, n)))
    monkeypatch.setattr(sknoded, "run_once", lambda p, n: None)

    def fake_sleep(seconds):
        raise RuntimeError("stop after first cycle")

    monkeypatch.setattr(sknoded.time, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="stop after first cycle"):
        sknoded.main_loop(paths, "node-41", interval=5, actuation_interval=5)
    assert calls == [(paths, "node-41")]
