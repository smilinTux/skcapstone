"""Allowlisted, schema-validating client for the frozen control-plane API."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Mapping
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACT_VERSION = "1.1.0"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EVENT_TOPICS = 16
REPORT_ID = re.compile(r"^rpt-[a-z0-9][a-z0-9-]{7,95}$")
METRIC_FAMILIES = frozenset(
    {
        "portfolio",
        "flow",
        "reliability",
        "delivery",
        "architecture",
        "ai",
        "economy",
        "governance",
        "experience",
    }
)
SCOPE_KEYS = frozenset({"project_id", "service_id", "environment", "baseline"})
PAGE_OPERATIONS = frozenset({"board", "fleet", "economy"})

_OPERATIONS = {
    "health": ("GET", "/api/v1/health", "envelope", False),
    "overview": ("GET", "/api/v1/overview", "envelope", True),
    "board": ("GET", "/api/v1/board/summary", "envelope", True),
    "fleet": ("GET", "/api/v1/fleet/summary", "envelope", True),
    "economy": ("GET", "/api/v1/economy/summary", "envelope", True),
    "insight": ("POST", "/api/v1/insights/query", "insight", True),
}


class ControlPlaneClientError(RuntimeError):
    """A response failed origin, transport, status, or schema validation."""


@dataclass(frozen=True)
class ClientResponse:
    data: dict
    etag: str | None
    not_modified: bool = False


def _contract_documents() -> dict[str, dict]:
    root = Path(__file__).parent / "contracts" / "v1.1.0"
    names = (
        "openapi.control-plane.v1.1.0.json",
        "control-plane-metric-result.v1.1.0.schema.json",
        "control-plane-recommendation.v1.1.0.schema.json",
        "control-plane-insight.v1.1.0.schema.json",
        "control-plane-report-snapshot.v1.1.0.schema.json",
    )
    return {name: json.loads((root / name).read_text(encoding="utf-8")) for name in names}


class ContractValidators:
    """Load the published schemas once and validate exact response families."""

    def __init__(self) -> None:
        documents = _contract_documents()
        resources = []
        for document in documents.values():
            if schema_id := document.get("$id"):
                resources.append((schema_id, Resource.from_contents(document)))
        self.registry = Registry().with_resources(resources)
        self.format_checker = FormatChecker()
        openapi = copy.deepcopy(documents["openapi.control-plane.v1.1.0.json"])
        openapi.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.skworld.local/skdashboard/openapi.control-plane.v1.1.0.json",
                "$ref": "#/components/schemas/ProjectionEnvelope",
            }
        )
        self.validators = {
            "envelope": Draft202012Validator(
                openapi, registry=self.registry, format_checker=self.format_checker
            ),
            "insight": Draft202012Validator(
                documents["control-plane-insight.v1.1.0.schema.json"],
                registry=self.registry,
                format_checker=self.format_checker,
            ),
            "report": Draft202012Validator(
                documents["control-plane-report-snapshot.v1.1.0.schema.json"],
                registry=self.registry,
                format_checker=self.format_checker,
            ),
        }
        query = copy.deepcopy(documents["openapi.control-plane.v1.1.0.json"])
        query.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.skworld.local/skdashboard/insight-query.v1.1.0.json",
                "$ref": "#/components/schemas/InsightQuery",
            }
        )
        self.validators["insight_query"] = Draft202012Validator(
            query, registry=self.registry, format_checker=self.format_checker
        )
        error = copy.deepcopy(documents["openapi.control-plane.v1.1.0.json"])
        error.update(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "$id": "https://schemas.skworld.local/skdashboard/error.v1.1.0.json",
                "$ref": "#/components/schemas/Error",
            }
        )
        self.validators["error"] = Draft202012Validator(
            error, registry=self.registry, format_checker=self.format_checker
        )

    def validate(self, family: str, value: object) -> None:
        try:
            self.validators[family].validate(value)
        except Exception as error:
            raise ControlPlaneClientError(f"{family} response failed schema validation") from error


class ControlPlaneClient:
    """Read only the frozen allowlist from one discovered same-origin API."""

    def __init__(
        self,
        origin: str,
        bearer: str,
        http: httpx.AsyncClient,
        *,
        manifest: Mapping[str, object],
        owns_http: bool,
        validators: ContractValidators | None = None,
    ) -> None:
        self.origin = origin.rstrip("/")
        self._bearer = bearer
        self._http = http
        self._owns_http = owns_http
        self.manifest = copy.deepcopy(dict(manifest))
        self.validators = validators or ContractValidators()
        self._cache: dict[str, ClientResponse] = {}

    def __repr__(self) -> str:
        return f"ControlPlaneClient(origin={self.origin!r})"

    @classmethod
    async def discover(
        cls,
        discovery_url: str,
        bearer: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 5.0,
    ) -> "ControlPlaneClient":
        parsed = urlsplit(discovery_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path != "/.well-known/skworld-module.json"
            or len(discovery_url) > 2048
        ):
            raise ControlPlaneClientError("discovery URL is not a canonical HTTPS manifest")
        if not bearer or len(bearer.encode("utf-8")) > 64 * 1024:
            raise ControlPlaneClientError("bearer is missing or exceeds its bound")
        http = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)
        try:
            response = await http.get(discovery_url, headers={"Accept": "application/json"})
            if response.status_code != 200 or len(response.content) > 64 * 1024:
                raise ControlPlaneClientError("control-plane discovery failed")
            manifest = response.json()
            origin = f"{parsed.scheme}://{parsed.netloc}"
            cls._validate_manifest(manifest, origin)
            return cls(origin, bearer, http, manifest=manifest, owns_http=True)
        except Exception:
            await http.aclose()
            raise

    @staticmethod
    def _validate_manifest(manifest: object, origin: str) -> None:
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != "1.1":
            raise ControlPlaneClientError("discovery manifest is incompatible")
        auth = manifest.get("auth")
        if not isinstance(auth, dict) or auth.get("audience") != "skdashboard":
            raise ControlPlaneClientError("discovery manifest has the wrong audience")
        health = manifest.get("health")
        if health != origin + "/api/v1/health":
            raise ControlPlaneClientError("discovery manifest has no canonical health route")
        entry = manifest.get("entry")
        entry_url = entry.get("url") if isinstance(entry, dict) else None
        parsed_entry = urlsplit(entry_url) if isinstance(entry_url, str) else None
        if parsed_entry is None or entry_url.rstrip("/") != origin:
            raise ControlPlaneClientError("discovery entry crosses origins")

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "ControlPlaneClient":
        return self

    async def __aexit__(self, *_args) -> None:
        await self.aclose()

    @staticmethod
    def _query(values: Mapping[str, object] | None, allowed: frozenset[str]) -> dict[str, str]:
        query = {}
        for key, value in (values or {}).items():
            if key not in allowed or not isinstance(value, str) or not value or len(value) > 512:
                raise ControlPlaneClientError("query is outside the frozen allowlist")
            query[key] = value
        return query

    async def _request(
        self,
        operation: str,
        *,
        params: Mapping[str, object] | None = None,
        body: dict | None = None,
        path: str | None = None,
        schema: str | None = None,
        protected: bool | None = None,
    ) -> ClientResponse:
        if operation in _OPERATIONS:
            method, fixed_path, family, requires_auth = _OPERATIONS[operation]
        elif operation == "report" and path is not None:
            method, fixed_path, family, requires_auth = "GET", path, "report", True
        else:
            raise ControlPlaneClientError("operation is not allowlisted")
        family = schema or family
        requires_auth = requires_auth if protected is None else protected
        url = self.origin + fixed_path
        headers = {"Accept": "application/json"}
        if requires_auth:
            headers["Authorization"] = f"Bearer {self._bearer}"
        cache_key = json.dumps([method, fixed_path, params or {}], sort_keys=True)
        if method == "GET" and cache_key in self._cache and self._cache[cache_key].etag:
            headers["If-None-Match"] = self._cache[cache_key].etag or ""
        encoded = None
        if body is not None:
            if operation == "insight":
                self.validators.validate("insight_query", body)
            encoded = json.dumps(body, allow_nan=False, sort_keys=True, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 64 * 1024:
                raise ControlPlaneClientError("request body exceeds its bound")
            headers["Content-Type"] = "application/json"
        response = await self._http.request(
            method, url, params=dict(params or {}), content=encoded, headers=headers
        )
        if response.status_code == 304:
            cached = self._cache.get(cache_key)
            if cached is None:
                raise ControlPlaneClientError("server returned 304 without a validated baseline")
            return ClientResponse(copy.deepcopy(cached.data), cached.etag, True)
        if response.status_code != 200:
            try:
                error = response.json()
            except ValueError as exc:
                raise ControlPlaneClientError("error response is not JSON") from exc
            self.validators.validate("error", error)
            raise ControlPlaneClientError(
                f"control-plane request failed: {response.status_code} {error['code']}"
            )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ControlPlaneClientError("response exceeds its read bound")
        if not response.headers.get("content-type", "").startswith("application/json"):
            raise ControlPlaneClientError("response media type is not JSON")
        try:
            data = response.json()
        except ValueError as error:
            raise ControlPlaneClientError("response is not JSON") from error
        self.validators.validate(family, data)
        result = ClientResponse(copy.deepcopy(data), response.headers.get("etag"))
        if method == "GET":
            self._cache[cache_key] = ClientResponse(copy.deepcopy(data), result.etag)
        return result

    async def health(self) -> ClientResponse:
        return await self._request("health")

    async def overview(self, scope: Mapping[str, object] | None = None) -> ClientResponse:
        return await self._request("overview", params=self._query(scope, SCOPE_KEYS))

    async def saved_scope(self, scope: Mapping[str, object]) -> ClientResponse:
        return await self.overview(scope)

    async def board(self, **query: str) -> ClientResponse:
        return await self._request(
            "board",
            params=self._query(
                query, frozenset({"project_id", "from", "to", "timezone", "cursor", "limit"})
            ),
        )

    async def fleet(self, **query: str) -> ClientResponse:
        return await self._request(
            "fleet", params=self._query(query, frozenset({"environment", "cursor", "limit"}))
        )

    async def economy(self, **query: str) -> ClientResponse:
        return await self._request(
            "economy",
            params=self._query(
                query,
                frozenset(
                    {"project_id", "measurement_lane", "from", "to", "timezone", "cursor", "limit"}
                ),
            ),
        )

    async def report(self, snapshot_id: str) -> ClientResponse:
        if not REPORT_ID.fullmatch(snapshot_id):
            raise ControlPlaneClientError("report snapshot id is invalid")
        return await self._request("report", path=f"/api/v1/reports/{snapshot_id}")

    async def insight(self, query: dict) -> ClientResponse:
        return await self._request("insight", body=query)

    async def metric_family(
        self, family: str, scope: Mapping[str, object] | None = None
    ) -> list[dict]:
        if family not in METRIC_FAMILIES:
            raise ControlPlaneClientError("metric family is not allowlisted")
        response = await (self.economy() if family == "economy" else self.overview(scope))
        return [
            copy.deepcopy(metric)
            for metric in response.data.get("metrics", [])
            if str(metric.get("metric_id", "")).startswith(f"{family}.")
        ]

    async def pages(
        self, operation: str, *, max_pages: int = 100, **query: str
    ) -> AsyncIterator[ClientResponse]:
        if operation not in PAGE_OPERATIONS or not 1 <= max_pages <= 100:
            raise ControlPlaneClientError("pagination request is outside its bound")
        cursor = query.pop("cursor", None)
        seen = set()
        for _ in range(max_pages):
            if cursor:
                if cursor in seen or len(cursor) > 512:
                    raise ControlPlaneClientError(
                        "pagination cursor repeated or exceeded its bound"
                    )
                seen.add(cursor)
                query["cursor"] = cursor
            response = await getattr(self, operation)(**query)
            yield response
            page = response.data.get("page") or {}
            cursor = page.get("next_cursor")
            if not page.get("has_more"):
                return
        raise ControlPlaneClientError("pagination exceeded its page bound")

    async def events(
        self, *, cursor: str | None = None, topics: tuple[str, ...] = ()
    ) -> list[dict]:
        if cursor is not None and (not cursor or len(cursor) > 512):
            raise ControlPlaneClientError("event cursor is invalid")
        if len(topics) > MAX_EVENT_TOPICS or any(not value or len(value) > 64 for value in topics):
            raise ControlPlaneClientError("event topics exceed their bound")
        params = {"topics": ",".join(topics)} if topics else {}
        if cursor:
            params["cursor"] = cursor
        response = await self._http.get(
            self.origin + "/api/v1/events",
            params=params,
            headers={"Accept": "text/event-stream", "Authorization": f"Bearer {self._bearer}"},
        )
        if response.status_code != 200 or len(response.content) > MAX_RESPONSE_BYTES:
            raise ControlPlaneClientError("event response failed or exceeded its bound")
        Draft202012Validator({"type": "string", "maxLength": MAX_RESPONSE_BYTES}).validate(
            response.text
        )
        events = []
        for block in response.text.split("\n\n"):
            lines = [line for line in block.splitlines() if line and not line.startswith(":")]
            if not lines:
                continue
            event = next(
                (line[6:] for line in lines if line.startswith("event:")), "message"
            ).strip()
            data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
            events.append({"event": event, "data": json.loads(data) if data else None})
        return events

    @staticmethod
    def evidence_refs(document: Mapping[str, object]) -> list[str]:
        found = set()

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "evidence_ref" and isinstance(child, str):
                        found.add(child)
                    elif key == "evidence_refs" and isinstance(child, list):
                        found.update(item for item in child if isinstance(item, str))
                    else:
                        visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(document)
        if len(found) > 256 or any(not value or len(value) > 512 for value in found):
            raise ControlPlaneClientError("evidence references exceed their bound")
        return sorted(found)


__all__ = [
    "ClientResponse",
    "ContractValidators",
    "ControlPlaneClient",
    "ControlPlaneClientError",
]
