from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from skcapstone.fleet_route_preflight import resolve_and_preflight

ROTATE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


class Response:
    def __init__(self, body: dict, headers: dict[str, str] | None = None):
        self.body = io.BytesIO(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int) -> bytes:
        return self.body.read(size)


def opener(catalog: list[dict], *, reject: bool = False):
    calls = []

    def open_(request, timeout):
        calls.append((request, timeout))
        if isinstance(request, str):
            return Response({"data": catalog})
        if reject:
            raise urllib.error.HTTPError(request.full_url, 502, "bad gateway", {}, None)
        payload = json.loads(request.data)
        assert payload["model"] == "sk-qwen"
        return Response(
            {"model": "backend-from-body", "choices": [{"message": {"content": "OK"}}]},
            {"x-sk-model-served": "qwen-current", "x-sk-backend": "local-qwen"},
        )

    return calls, open_


def test_alias_change_uses_current_logical_route_and_records_identities():
    calls, open_ = opener([{"id": "sk-qwen", "provider": "old-catalog-value"}])
    result = resolve_and_preflight("http://gateway", "sk-qwen", opener=open_)
    assert result.to_dict() == {
        "requested_identity": "sk-qwen",
        "served_identity": "qwen-current",
        "provider": "local-qwen",
    }
    assert len(calls) == 2


@pytest.mark.parametrize(
    "catalog",
    [
        [{"id": "qwen3.8-stale", "stale": False}],
        [{"id": "sk-qwen", "stale": True}],
        [{"id": "sk-qwen", "advertised": False}],
    ],
)
def test_stale_or_unadvertised_catalog_entry_fails_before_dispatch(catalog):
    calls, open_ = opener(catalog)
    with pytest.raises(ValueError, match="not currently advertised"):
        resolve_and_preflight("http://gateway", "sk-qwen", opener=open_)
    assert len(calls) == 1


def test_backend_rejection_is_concise_and_non_secret():
    _calls, open_ = opener([{"id": "sk-qwen"}], reject=True)
    with pytest.raises(ValueError, match="HTTP 502") as error:
        resolve_and_preflight("http://gateway", "sk-qwen", opener=open_)
    assert "Reply OK" not in str(error.value)


def test_automatic_preflight_precedes_workspace_and_claim():
    source = ROTATE.read_text(encoding="utf-8")
    probe = source.index("_route_plan=build_worker_route_plan")
    workspace = source.index("default_workspace=os.path.join", probe)
    claim = source.index('claim=subprocess.run([SKC,"coord","claim"', workspace)
    assert probe < workspace < claim
