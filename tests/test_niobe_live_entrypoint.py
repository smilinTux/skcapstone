import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.niobe_live_entrypoint import run_live


def activation(card_revision: str = "a" * 64) -> dict:
    return {
        "schema": "skfleet.niobe-activation/v1",
        "state": "active",
        "seat": "niobe",
        "decision_id": "casey-c4e7a9b2-20260906",
        "authorized_by": "casey",
        "card_id": "c4e7a9b2",
        "card_revision": card_revision,
        "host": "chiap08",
        "live_unit": "skfleet-niobe-live.timer",
        "product_scope": ["skcapstone", "skdashboard", "skworld"],
        "card_label": "seat-niobe",
        "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
        "denied_actions": ["merge", "deploy", "application_actuation", "external_dispatch"],
        "expires_at": "2099-10-06T22:00:00+00:00",
        "rollback": {
            "owner": "casey",
            "action": "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer",
        },
    }


def test_live_wrapper_validates_then_runs_exact_dispatcher(tmp_path: Path, monkeypatch) -> None:
    core = tmp_path / "home/cards/c4e7a9b2/core.json"
    core.parent.mkdir(parents=True)
    core.write_text('{"id":"c4e7a9b2"}\n')
    revision = hashlib.sha256(core.read_bytes()).hexdigest()
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    calls = []
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: None)
    rc = run_live(
        activation_path=path,
        dispatcher=dispatcher,
        local_host="chiap08",
        runner=lambda command, check, env: (
            calls.append((command, check, env)) or SimpleNamespace(returncode=0)
        ),
    )
    assert rc == 0
    assert calls[0][:2] == ([sys.executable, str(dispatcher), "--go"], False)
    assert calls[0][2]["SKFLEET_NIOBE_ACTIVATION"] == str(path.resolve())


def test_live_wrapper_refuses_wrong_host(tmp_path: Path) -> None:
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation()))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="inactive host"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap01")


def test_live_wrapper_refuses_stale_well_formed_card_revision(tmp_path: Path) -> None:
    core = tmp_path / "home/cards/c4e7a9b2/core.json"
    core.parent.mkdir(parents=True)
    core.write_text('{"id":"c4e7a9b2"}\n')
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation("b" * 64)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="revision is stale"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08")
