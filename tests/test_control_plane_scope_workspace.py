from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.datastructures import QueryParams
from starlette.testclient import TestClient

from skdashboard.control_plane_scope import (
    ProtectedScopeDenied,
    ScopeQueryError,
    parse_now_scope,
)
from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]
HEADERS = {
    "Authorization": "Bearer valid-read",
    "Origin": "http://10.0.0.139:7778",
}


def _authorizer(bearer: str, capability: str, _target: str) -> bool:
    return bearer == "valid-read" and capability == "skdashboard.read"


def test_scope_parser_accepts_only_the_exact_nonsecret_v1_contract() -> None:
    parsed = parse_now_scope(
        QueryParams(
            "role=architect&scope=estate&window=latest&baseline=none&service=all"
            "&selected_silo=legal&truth=partial&saved_view=sv-0123456789abcdef0123456789abcdef"
        )
    )
    assert parsed.as_dict() == {
        "role": "architect",
        "scope": "estate",
        "window": "latest",
        "baseline": "none",
        "service": "all",
        "selected_silo": "legal",
        "truth": "partial",
        "saved_view": "sv-0123456789abcdef0123456789abcdef",
    }

    for query in (
        "scope=estate&scope=estate",
        "scope=project",
        "token=secret-value",
        "selected_silo=missing",
        "truth=healthy",
        "role=owner",
        f"saved_view={'x' * 129}",
    ):
        with pytest.raises(ScopeQueryError):
            parse_now_scope(QueryParams(query))
    for query in ("tenant_id=one", "matter_id=one", "tenant_id=one&matter_id=two"):
        with pytest.raises(ProtectedScopeDenied, match="protected scope is not available"):
            parse_now_scope(QueryParams(query))


def test_overview_echoes_normalized_scope_and_rejects_before_retrieval(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=_authorizer))
    valid = (
        "/api/v1/overview?role=project-manager&scope=estate&window=latest"
        "&baseline=none&service=all&selected_silo=flow&truth=current"
    )
    response = client.get(valid, headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["scope"] == {
        "role": "project-manager",
        "scope": "estate",
        "window": "latest",
        "baseline": "none",
        "service": "all",
        "selected_silo": "flow",
        "truth": "current",
    }

    secret = "do-not-echo-this-value"
    with patch("skdashboard.control_plane_adapters.default_readers") as readers:
        invalid_responses = [
            client.get(path, headers=HEADERS)
            for path in (
                f"/api/v1/overview?token={secret}",
                "/api/v1/overview?scope=estate&scope=estate",
                "/api/v1/overview?scope=project",
                f"/api/v1/overview?selected_silo={'x' * 129}",
            )
        ]
        protected_responses = [
            client.get(f"/api/v1/overview?tenant_id={secret}", headers=HEADERS),
            client.get(f"/api/v1/overview?matter_id={secret}", headers=HEADERS),
        ]
    readers.assert_not_called()
    assert {item.status_code for item in invalid_responses} == {400}
    assert {item.json()["code"] for item in invalid_responses} == {"INVALID_SCOPE"}
    assert {item.status_code for item in protected_responses} == {403}
    assert {item.headers["cache-control"] for item in protected_responses} == {"no-store"}
    assert {item.json()["code"] for item in protected_responses} == {"PROTECTED_SCOPE_DENIED"}
    assert all(secret not in item.text for item in [*invalid_responses, *protected_responses])


def test_browser_contract_is_bounded_read_only_and_accessible() -> None:
    html = (ROOT / "src/skdashboard/static/overview.html").read_text(encoding="utf-8")
    helper = (ROOT / "src/skdashboard/static/js/control_plane_scope.js").read_text(
        encoding="utf-8"
    )
    overview = (ROOT / "src/skdashboard/static/js/overview.js").read_text(encoding="utf-8")
    qualifier = (ROOT / "scripts/qualify_control_plane_now_cdp.mjs").read_text(encoding="utf-8")

    for marker in (
        'id="saved-view-select"',
        'id="share-link"',
        'id="command-palette"',
        'role="combobox"',
        'role="listbox"',
        "Saved views expire after 24 hours",
    ):
        assert marker in html
    for marker in (
        "URL_KEYS",
        "PROTECTED_KEYS",
        "SECRET_KEY",
        "MAX_VIEWS = 8",
        "VIEW_TTL_MS = 24 * 60 * 60 * 1000",
        "created > now + MAX_CLOCK_SKEW_MS",
        "registry_hash",
        "exactKeys(view, VIEW_KEYS)",
    ):
        assert marker in helper
    assert "fetch(" not in helper
    assert "getJSON(apiUrl(context))" in overview
    assert "responseMatches(response, context)" in overview
    assert "epoch !== loadEpoch" in overview
    assert "PopStateEvent" in qualifier
    assert "401 revocation did not fail closed" in qualifier
    assert "403 revocation did not fail closed" in qualifier
    assert "Response scope did not match" in qualifier
    assert "Expired view did not fail closed" in qualifier
    assert "Tampered view did not fail closed" in qualifier
    assert "Future-issued view did not fail closed" in qualifier
    assert 'request.method !== "GET"' in qualifier
