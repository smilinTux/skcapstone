"""Tests for the trust-graph dashboard panel (dashboard + trust_graph fold-in)."""

from __future__ import annotations

import json

import pytest

from skcapstone.dashboard import _trust_graph_dict, create_app


@pytest.fixture
def home(tmp_path):
    """A home with an identity, an operator, and two board collaborators.

    Produces a self node plus ``coord`` edges (and an ``operator`` edge), which
    exercises the multi-node / multi-edge-type path of ``build_trust_graph``.
    """
    ident_dir = tmp_path / "identity"
    ident_dir.mkdir(parents=True)
    (ident_dir / "identity.json").write_text(
        json.dumps({"name": "lumina", "fingerprint": "ABC123", "capauth_managed": True}),
        encoding="utf-8",
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "name": "lumina",
                "operator": {"name": "Chef", "fingerprint": "OP-1", "entity_type": "human"},
            }
        ),
        encoding="utf-8",
    )
    agents = tmp_path / "coordination" / "agents"
    agents.mkdir(parents=True)
    (agents / "opus.json").write_text(
        json.dumps({"agent": "opus", "state": "active", "completed_tasks": [1, 2, 3]}),
        encoding="utf-8",
    )
    (agents / "jarvis.json").write_text(
        json.dumps({"agent": "jarvis", "state": "idle", "completed_tasks": []}),
        encoding="utf-8",
    )
    return tmp_path


def test_trust_graph_dict_shape(home):
    d = _trust_graph_dict(home)
    assert isinstance(d["nodes"], list) and isinstance(d["edges"], list)
    ids = {n["id"] for n in d["nodes"]}
    assert "lumina" in ids and "opus" in ids and "jarvis" in ids
    # every node carries the documented keys
    for n in d["nodes"]:
        assert {"id", "label", "type"} <= set(n)
    # coord edges from self to the two collaborators
    coord = [e for e in d["edges"] if e["type"] == "coord"]
    assert {e["target"] for e in coord} >= {"opus", "jarvis"}
    assert d["stats"]["nodes"] == len(d["nodes"])


def test_empty_home_is_empty_not_error(tmp_path):
    d = _trust_graph_dict(tmp_path)
    assert d["nodes"] == [] and d["edges"] == []
    assert "error" not in d


def test_build_failure_degrades_gracefully(tmp_path, monkeypatch):
    import capauth.trust.graph as tg

    def boom(_home):
        raise RuntimeError("source unreadable")

    monkeypatch.setattr(tg, "build_trust_graph", boom)
    d = _trust_graph_dict(tmp_path)
    assert d["nodes"] == [] and d["edges"] == []
    assert "unavailable" in d.get("note", "")


# ---- HTTP routes ----


def test_api_trust_graph_route(home):
    from starlette.testclient import TestClient

    client = TestClient(create_app(home))
    r = client.get("/api/trust/graph")
    assert r.status_code == 200
    body = r.json()
    assert "nodes" in body and "edges" in body and "stats" in body
    assert any(n["id"] == "opus" for n in body["nodes"])


def test_trust_page_serves_html(home):
    from starlette.testclient import TestClient

    client = TestClient(create_app(home))
    r = client.get("/trust")
    assert r.status_code == 200
    assert "Trust Graph" in r.text
    assert "/api/trust/graph" in r.text  # the page fetches the data endpoint


def test_empty_home_route_ok(tmp_path):
    from starlette.testclient import TestClient

    client = TestClient(create_app(tmp_path))
    r = client.get("/api/trust/graph")
    assert r.status_code == 200
    assert r.json()["nodes"] == []
