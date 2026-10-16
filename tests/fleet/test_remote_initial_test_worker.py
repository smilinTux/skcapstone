import json
import socket

from skcapstone.fleet import production_tests
from skcapstone.fleet import remote_initial_test_worker as worker


def test_remote_initial_worker_scopes_remote_host_validation(tmp_path, monkeypatch):
    host = socket.gethostname().split(".")[0].lower()
    binding = {"source_card": "1234abcd", "source_head": "head"}
    plan_path = tmp_path / "fleet/test-plans/card.initial.next.json"
    directory = tmp_path / "fleet/test-runs" / ("a" * 64)
    initial_path = plan_path.with_name("card.initial.json")
    initial_path.parent.mkdir(parents=True)
    initial_path.parent.chmod(0o700)
    initial_path.write_text("{}")
    initial_path.chmod(0o600)
    launch = {
        "binding": binding,
        "request_id": "a" * 64,
        "policy": {"authority_host": host},
    }
    sealed = {"initial_profile_qualification": True, "host": host}
    calls = []

    def load_plan(request_home, request_binding, **kwargs):
        calls.append((request_home, request_binding, kwargs))
        return sealed, plan_path, "a" * 64

    def run_module(name, run_name):
        assert name == "skcapstone.fleet.production_test_worker"
        assert run_name == "__main__"
        production_tests.load_plan(tmp_path, binding)
        production_tests.load_plan(tmp_path, {"source_card": "other"})

    monkeypatch.setattr(worker.sys, "argv", ["worker", str(plan_path), str(directory)])
    monkeypatch.setattr(worker.production_tests, "read_json", lambda *_args: launch)
    monkeypatch.setattr(worker.production_tests, "load_plan", load_plan)
    monkeypatch.setattr(worker.plan, "_initial_plan_base", lambda *_args: initial_path)
    monkeypatch.setattr(
        worker.plan,
        "read_private",
        lambda *_args: json.dumps(
            {
                "remote_initial_profile_qualification": True,
                "authority_host": host,
                "host": "chiap02" if host != "chiap02" else "chiap01",
            }
        ).encode(),
    )
    monkeypatch.setattr(worker.plan, "run_directory", lambda *_args: directory)
    monkeypatch.setattr(worker.runpy, "run_module", run_module)

    worker.main()

    assert calls[0][2] == {"allow_remote_host": True}
    assert calls[1][1] == binding
    assert calls[1][2] == {"allow_remote_host": True}
    assert calls[2][1] == {"source_card": "other"}
    assert calls[2][2] == {}
