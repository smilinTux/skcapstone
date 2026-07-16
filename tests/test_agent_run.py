"""Tests for the Phase 4 AI next-steps runner (agent_run)."""
from __future__ import annotations

import pytest

from skcapstone import agent_run as ar
from skcapstone.card_store import CardStore, import_from_legacy
from skcapstone.coordination import Board, Task


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="t1", title="Wire 2FA verify", created_by="opus"))
    board.create_task(Task(id="c1", title="Cutover change", created_by="opus", tags=["change"]))
    import_from_legacy(tmp_path)
    return tmp_path


def test_request_run_attaches_queued(home):
    r = ar.request_run(home, "t1", "add tests and open a PR", agent="opus", mode="propose")
    assert r["ok"] and r["state"] == "queued"
    run = ar.current_run(home, "t1")
    assert run["state"] == "queued" and run["mode"] == "propose"
    assert run["instruction"].startswith("add tests")


def test_request_run_validation(home):
    assert "error" in ar.request_run(home, "t1", "x", mode="bogus")
    assert "error" in ar.request_run(home, "t1", "   ")
    assert "error" in ar.request_run(home, "nope", "do it")


def test_list_queued(home):
    ar.request_run(home, "t1", "do it")
    q = ar.list_queued(home)
    assert any(i["card_id"] == "t1" for i in q)


def test_gate_propose_and_execute():
    assert ar.gate("task", "propose")["allow_execute"] is True
    assert ar.gate("task", "dry-run")["allow_execute"] is True
    assert ar.gate("task", "execute")["allow_execute"] is True
    # change tickets cannot self-execute
    assert ar.gate("change", "execute")["allow_execute"] is False


def test_process_one_no_live_records_plan(home):
    ar.request_run(home, "t1", "do the thing", mode="propose")
    item = ar.list_queued(home)[0]
    out = ar.process_one(home, item)
    assert out["state"] == "needs-review" and out.get("planned")
    run = ar.current_run(home, "t1")
    assert run["state"] == "needs-review"
    assert any(a["atype"] == "response" for a in run["activity"])
    # card moved to review
    assert CardStore(home).fold("t1").status.value == "review"


def test_process_one_change_execute_is_gated(home):
    # a change-kind ticket in execute mode must be gated (needs-review, not executed).
    # (kind=change comes from ITIL change cards; simulate that kind on the item.)
    ar.request_run(home, "c1", "deploy it", mode="execute")
    run = ar.current_run(home, "c1")
    item = {"card_id": "c1", "kind": "change", "run": run}
    out = ar.process_one(home, item)
    assert out["state"] == "needs-review" and out.get("gated") is True
    run = ar.current_run(home, "c1")
    assert any(a["atype"] == "elicitation" for a in run["activity"])


def test_process_one_live_dispatch(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    ar.request_run(home, "t1", "run it", mode="dry-run")
    item = ar.list_queued(home)[0]
    calls = {}

    def dispatcher(ctx):
        calls["ctx"] = ctx
        return {"summary": "did the thing", "activity": [{"atype": "action", "text": "ran tests"}],
                "links": {"pr": "#42"}}

    out = ar.process_one(home, item, dispatcher=dispatcher)
    assert out["state"] == "needs-review"
    assert calls["ctx"]["instruction"] == "run it"
    run = ar.current_run(home, "t1")
    assert run["links"].get("pr") == "#42"
    assert any(a["text"] == "ran tests" for a in run["activity"])


def test_run_ai_runner_job_smoke(home, monkeypatch):
    # zero-arg job entrypoint should process queued runs against SHARED_ROOT
    import skcapstone
    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(home), raising=False)
    ar.request_run(home, "t1", "do it", mode="propose")
    ar.run_ai_runner_job()  # no exception; records a plan
    assert ar.current_run(home, "t1")["state"] == "needs-review"


def test_queue_ai_endpoint_and_capability(home, monkeypatch):
    from starlette.testclient import TestClient
    from skcapstone.dashboard import create_app
    client = TestClient(create_app(home))

    # open (no token configured) -> queues
    r = client.post("/api/card/t1/queue-ai",
                    json={"instruction": "add tests", "agent": "opus", "mode": "propose"},
                    headers={"X-SK-Actor": "chef"})
    assert r.status_code == 200 and r.json()["ok"]
    run = client.get("/api/card/t1").json()["card"]["meta"]["agent_run"]
    assert run["state"] == "queued" and run["agent"] == "opus"
    assert run["requester"] == "chef"

    # with a token configured -> must present it
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "s3cret")
    bad = client.post("/api/card/t1/queue-ai", json={"instruction": "x"})
    assert bad.status_code == 403
    good = client.post("/api/card/t1/queue-ai", json={"instruction": "x"},
                       headers={"X-SK-Capability": "s3cret"})
    assert good.status_code == 200 and good.json()["ok"]


def test_suggest_heuristic(home):
    d = ar.suggest_next_steps(home, "t1", use_llm=False)
    assert d["source"] == "heuristic"
    assert len(d["suggestions"]) >= 3
    assert all("text" in s and s["mode"] in ("propose", "dry-run", "execute") for s in d["suggestions"])


def test_ensure_card_and_suggest_for_itil(tmp_path):
    from skcapstone.itil import ITILManager
    mgr = ITILManager(tmp_path)
    inc = mgr.create_incident(title="skmem-pg down", severity="sev2", created_by="lumina")
    # incident is not yet a CardStore card; suggest should materialize it
    d = ar.suggest_next_steps(tmp_path, inc.id, use_llm=False)
    assert d["suggestions"] and d["source"] == "heuristic"
    # incident heuristics mention investigation
    assert any("investigate" in s["text"].lower() or "root cause" in s["text"].lower()
               for s in d["suggestions"])
    # and we can queue an AI run on the ITIL ticket
    r = ar.request_run(tmp_path, inc.id, "investigate root cause", mode="propose")
    assert r["ok"]
    assert ar.current_run(tmp_path, inc.id)["state"] == "queued"


def test_change_suggestions_never_execute(tmp_path):
    from skcapstone.itil import ITILManager
    mgr = ITILManager(tmp_path)
    ch = mgr.propose_change(title="Gateway cutover", created_by="lumina")
    d = ar.suggest_next_steps(tmp_path, ch.id, use_llm=False)
    assert all(s["mode"] != "execute" for s in d["suggestions"])


def test_suggestions_route(home):
    from starlette.testclient import TestClient
    from skcapstone.dashboard import create_app
    client = TestClient(create_app(home))
    d = client.get("/api/card/t1/ai-suggestions?llm=0").json()
    assert d["suggestions"] and d["source"] == "heuristic"
