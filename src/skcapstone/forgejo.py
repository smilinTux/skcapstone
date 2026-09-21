"""Bounded transport for the explicitly authorized private SKLegal forge."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SKGIT_ORIGIN = "https://skgit.skstack01.douno.it"
SKGIT_REPOSITORY = SKGIT_ORIGIN + "/smilinTux/sklegal"
_ROOT = "/api/v1/repos/smilinTux/sklegal"
_MAX_BYTES = 4 * 1024 * 1024
_MAX_PAGES = 100
_SHA = re.compile(r"[0-9a-f]{40}")


class ForgejoError(ValueError):
    """A private-forge request could not be safely completed."""


class _NoRedirect(HTTPRedirectHandler):
    """Never forward authentication through a server-supplied redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse every redirect, including a redirect to the same origin."""
        return None


class ForgejoClient:
    """Keep one credential confined to one explicitly authorized origin."""

    def __init__(self, origin: str, token: str):
        if origin != SKGIT_ORIGIN:
            raise ForgejoError("forge_origin_not_authorized")
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ForgejoError("forge_credential_unavailable")
        self._origin, self._token = origin, token
        self._opener = build_opener(_NoRedirect())

    def request(self, method: str, path: str, payload: dict | None = None) -> Any:
        """Send bounded JSON without exposing authentication in error details."""
        parsed = urlsplit(path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not path.startswith("/api/v1/")
            or "%" in parsed.path
            or "\\" in path
            or ".." in parsed.path
            or any(key not in {"page", "limit", "state"} for key in query)
            or any(len(values) != 1 for values in query.values())
            or any(not query[key][0].isdigit() for key in ("page", "limit") if key in query)
            or ("state" in query and query["state"][0] != "open")
            or not (
                parsed.path in {"/api/v1/user", "/api/v1/user/teams"}
                or parsed.path.startswith(_ROOT + "/")
            )
        ):
            raise ForgejoError("forge_path_not_authorized")
        if method == "POST":
            if not re.fullmatch(re.escape(_ROOT) + r"/pulls/[1-9][0-9]*/reviews", parsed.path):
                raise ForgejoError("forge_write_not_authorized")
            if parsed.query or not isinstance(payload, dict):
                raise ForgejoError("forge_review_request_invalid")
            if (
                set(payload) != {"commit_id", "event", "body"}
                or not isinstance(payload["commit_id"], str)
                or not _SHA.fullmatch(payload["commit_id"])
                or payload["event"] != "APPROVED"
                or not isinstance(payload["body"], str)
                or not payload["body"].strip()
                or len(payload["body"]) > 20000
            ):
                raise ForgejoError("forge_review_request_invalid")
        elif method != "GET" or payload is not None:
            raise ForgejoError("forge_method_not_authorized")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self._origin + path,
            data=data,
            method=method,
            headers={
                "Authorization": "token " + self._token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=20) as response:
                raw = response.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                raise ForgejoError("forge_response_too_large")
            return json.loads(raw)
        except HTTPError as exc:
            raise ForgejoError(f"forge_http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise ForgejoError("forge_transport_unavailable") from None
        except (UnicodeError, json.JSONDecodeError):
            raise ForgejoError("forge_response_malformed") from None

    def pages(self, path: str) -> list[dict[str, Any]]:
        """Read all bounded pages, never interpreting a short page as complete."""
        return _pages(self, path)


def _pages(client: Any, path: str) -> list[dict[str, Any]]:
    """Reject repetition and excessive pagination rather than return partial data."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    separator = "&" if "?" in path else "?"
    for page in range(1, _MAX_PAGES + 1):
        values = client.request("GET", f"{path}{separator}page={page}&limit=50")
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            raise ForgejoError("forge_page_malformed")
        if not values:
            return rows
        digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        if digest in seen:
            raise ForgejoError("forge_pagination_repeated")
        seen.add(digest)
        rows.extend(values)
    raise ForgejoError("forge_pagination_limit")


def _pin(raw: Any, field: str) -> str:
    """Require a full commit pin from one normalized PR record."""
    nested = raw.get(field) if isinstance(raw, dict) else None
    sha = nested.get("sha") if isinstance(nested, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ForgejoError("forge_pr_sha_malformed")
    return sha


class ForgejoReadOnlyConnector:
    """Normalize private PRs for the existing mediated observation contract."""

    def __init__(self, client: ForgejoClient | None = None):
        self._client = client

    def list_open(self, repository: str) -> list[dict[str, Any]]:
        """Read private PRs and latest statuses, then recheck their exact heads."""
        if repository != SKGIT_REPOSITORY:
            raise ForgejoError("forge_repository_not_authorized")
        client = self._client or ForgejoClient(
            SKGIT_ORIGIN, os.environ.get("SKFLEET_SKGIT_READ_TOKEN", "")
        )
        rows = _pages(client, _ROOT + "/pulls?state=open")
        result: list[dict[str, Any]] = []
        numbers: set[int] = set()
        for raw in rows:
            number = raw.get("number")
            if type(number) is not int or number < 1 or number in numbers:
                raise ForgejoError("forge_pr_number_invalid")
            numbers.add(number)
            head, base = _pin(raw, "head"), _pin(raw, "base")
            statuses = _pages(client, f"{_ROOT}/commits/{head}/statuses")
            latest: dict[str, tuple[int, str]] = {}
            for status in statuses:
                context, identity, value = (status.get(k) for k in ("context", "id", "status"))
                if (
                    not isinstance(context, str)
                    or not context
                    or type(identity) is not int
                    or identity < 1
                    or not isinstance(value, str)
                    or not value
                ):
                    raise ForgejoError("forge_status_malformed")
                prior = latest.get(context)
                if prior is not None and prior[0] == identity and prior[1] != value:
                    raise ForgejoError("forge_status_conflict")
                if prior is None or identity > prior[0]:
                    latest[context] = (identity, value)
            current = client.request("GET", f"{_ROOT}/pulls/{number}")
            if (
                not isinstance(current, dict)
                or type(current.get("number")) is not int
                or current.get("number") != number
            ):
                raise ForgejoError("forge_pr_identity_changed")
            if _pin(current, "head") != head or _pin(current, "base") != base:
                raise ForgejoError("forge_pr_head_changed")
            item = dict(current)
            item["checks"] = [
                {"context": name, "status": value} for name, (_, value) in sorted(latest.items())
            ]
            item["snapshot_at"] = datetime.now(timezone.utc).isoformat()
            result.append(item)
        return result


class MultiForgeReadOnlyConnector:
    """Route only the explicitly authorized private repository away from GitHub."""

    def list_open(self, repository: str) -> list[dict[str, Any]]:
        """Keep private credential failures in the producer's existing error contract."""
        from .link_observation_producer import GhReadOnlyConnector, ProducerError

        if repository == SKGIT_REPOSITORY:
            try:
                return ForgejoReadOnlyConnector().list_open(repository)
            except ForgejoError as exc:
                raise ProducerError(str(exc)) from None
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ProducerError("repository_not_authorized")
        return list(GhReadOnlyConnector().list_open(repository))
