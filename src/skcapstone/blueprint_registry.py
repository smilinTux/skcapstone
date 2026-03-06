"""
Blueprint Registry Client — interact with the souls.skworld.io API.

This is a client library for the remote soul blueprint registry hosted at
souls.skworld.io. The actual server is a separate service; this module
provides a typed Python interface for listing, searching, publishing,
and downloading blueprints.

Authentication uses DID-based bearer tokens: the agent's DID key is
sent as ``Authorization: Bearer did:key:<fingerprint>`` so the registry
can attribute published blueprints to a sovereign identity.

No external dependencies — uses only ``urllib`` from the standard library.

Usage::

    from skcapstone.blueprint_registry import BlueprintRegistryClient

    client = BlueprintRegistryClient()
    blueprints = client.list_blueprints()
    result = client.search_blueprints("comedy")
    client.publish_blueprint(soul_data)
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skcapstone.blueprint_registry")

DEFAULT_BASE_URL = "https://souls.skworld.io/api"

# Timeout for HTTP requests (seconds).
_REQUEST_TIMEOUT = 30


class BlueprintRegistryError(Exception):
    """Raised when a registry API call fails."""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BlueprintRegistryClient:
    """Client for the souls.skworld.io blueprint registry API.

    Args:
        base_url: API base URL (default: ``https://souls.skworld.io/api``).
        did_key: DID key string for authentication (e.g. ``did:key:z6Mk...``).
            If not provided, the client will attempt to load it from the
            agent's identity on disk at ``~/.skcapstone/did/``.
        timeout: HTTP request timeout in seconds (default: 30).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        did_key: Optional[str] = None,
        timeout: int = _REQUEST_TIMEOUT,
    ) -> None:
        # Strip trailing slash for consistent URL joining
        self.base_url = base_url.rstrip("/")
        self.did_key = did_key
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    def _resolve_did_key(self) -> Optional[str]:
        """Resolve the DID key from the instance or from disk.

        Returns:
            DID key string, or None if unavailable.
        """
        if self.did_key:
            return self.did_key

        # Attempt to load from the agent's DID directory
        did_dir = Path.home() / ".skcapstone" / "did"
        key_file = did_dir / "did_key.json"
        if key_file.exists():
            try:
                data = json.loads(key_file.read_text(encoding="utf-8"))
                resolved = data.get("did") or data.get("did_key") or data.get("id")
                if resolved:
                    logger.debug("Resolved DID key from %s", key_file)
                    return resolved
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("Could not load DID key from %s: %s", key_file, exc)

        # Fallback: check identity.json
        identity_file = did_dir / "identity.json"
        if identity_file.exists():
            try:
                data = json.loads(identity_file.read_text(encoding="utf-8"))
                resolved = data.get("did_key") or data.get("did") or data.get("id")
                if resolved:
                    return resolved
            except (json.JSONDecodeError, OSError):
                pass

        return None

    def _build_headers(self, authenticated: bool = False) -> dict[str, str]:
        """Build HTTP headers for a request.

        Args:
            authenticated: Whether to include the Authorization header.

        Returns:
            Header dict.
        """
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "skcapstone-blueprint-registry/1.0",
        }
        if authenticated:
            did = self._resolve_did_key()
            if did:
                headers["Authorization"] = f"Bearer {did}"
            else:
                logger.warning(
                    "No DID key available for authenticated request. "
                    "Set did_key on the client or ensure ~/.skcapstone/did/ "
                    "contains a valid identity."
                )
        return headers

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict[str, Any]] = None,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        """Execute an HTTP request against the registry API.

        Args:
            method: HTTP method (GET, POST, etc.).
            path: URL path relative to base_url (e.g. ``/blueprints``).
            body: JSON body for POST/PUT requests.
            authenticated: Whether to attach DID auth header.

        Returns:
            Parsed JSON response as a dict.

        Raises:
            BlueprintRegistryError: On HTTP or connection errors.
        """
        url = f"{self.base_url}{path}"
        headers = self._build_headers(authenticated=authenticated)

        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, headers=headers, method=method
        )

        logger.debug("%s %s", method, url)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            msg = f"Registry API error {exc.code} for {method} {path}"
            if error_body:
                msg += f": {error_body[:500]}"
            raise BlueprintRegistryError(msg, status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise BlueprintRegistryError(
                f"Cannot reach registry at {url}: {exc.reason}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise BlueprintRegistryError(
                f"Invalid JSON in response from {method} {path}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def list_blueprints(self) -> list[dict[str, Any]]:
        """List all published blueprints in the registry.

        Calls ``GET /blueprints``.

        Returns:
            List of blueprint summary dicts, each containing at minimum
            ``name``, ``display_name``, ``category``, and ``soul_id``.

        Raises:
            BlueprintRegistryError: On API failure.
        """
        result = self._request("GET", "/blueprints")
        # API may return {"blueprints": [...]} or a bare list
        if isinstance(result, list):
            return result
        return result.get("blueprints", [])

    def get_blueprint(self, soul_id: str) -> dict[str, Any]:
        """Get a single blueprint by its soul ID.

        Calls ``GET /blueprints/{soul_id}``.

        Args:
            soul_id: The unique identifier (slug) of the blueprint.

        Returns:
            Full blueprint dict with all fields.

        Raises:
            BlueprintRegistryError: On API failure or 404.
        """
        return self._request("GET", f"/blueprints/{soul_id}")

    def publish_blueprint(self, soul_data: dict[str, Any]) -> dict[str, Any]:
        """Publish a soul blueprint to the registry.

        Calls ``POST /blueprints`` with DID-based authentication.

        The ``soul_data`` should conform to the SoulBlueprint schema
        (at minimum: ``name``, ``display_name``, ``category``).

        Args:
            soul_data: Blueprint data dict to publish.

        Returns:
            API response dict (typically includes ``soul_id`` and ``status``).

        Raises:
            BlueprintRegistryError: On API failure or auth error.
        """
        return self._request(
            "POST", "/blueprints", body=soul_data, authenticated=True
        )

    def search_blueprints(self, query: str) -> list[dict[str, Any]]:
        """Search for blueprints by a text query.

        Calls ``GET /blueprints/search?q=<query>``.

        Args:
            query: Search string (matched against name, category, traits, etc.).

        Returns:
            List of matching blueprint dicts.

        Raises:
            BlueprintRegistryError: On API failure.
        """
        # URL-encode the query parameter
        encoded_q = urllib.request.quote(query, safe="")
        result = self._request("GET", f"/blueprints/search?q={encoded_q}")
        if isinstance(result, list):
            return result
        return result.get("blueprints", result.get("results", []))

    def download_blueprint(self, soul_id: str) -> dict[str, Any]:
        """Download a full blueprint for local installation.

        Calls ``GET /blueprints/{soul_id}/download``.

        The returned dict can be written directly to a JSON file and
        loaded by the local SoulManager.

        Args:
            soul_id: The unique identifier (slug) of the blueprint.

        Returns:
            Complete blueprint data suitable for local installation.

        Raises:
            BlueprintRegistryError: On API failure or 404.
        """
        return self._request("GET", f"/blueprints/{soul_id}/download")

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def download_and_install(self, soul_id: str, home: Optional[Path] = None) -> Path:
        """Download a blueprint and install it locally.

        Args:
            soul_id: Registry soul ID to download.
            home: Agent home directory (default: ``~/.skcapstone``).

        Returns:
            Path to the installed blueprint JSON file.

        Raises:
            BlueprintRegistryError: On download failure.
        """
        bp_data = self.download_blueprint(soul_id)
        home = home or Path.home() / ".skcapstone"
        installed_dir = home / "soul" / "installed"
        installed_dir.mkdir(parents=True, exist_ok=True)

        name = bp_data.get("name", soul_id)
        dest = installed_dir / f"{name}.json"
        dest.write_text(json.dumps(bp_data, indent=2), encoding="utf-8")
        logger.info("Installed blueprint '%s' from registry to %s", soul_id, dest)
        return dest

    def publish_from_file(self, path: Path) -> dict[str, Any]:
        """Load a local blueprint file and publish it to the registry.

        Args:
            path: Path to a local blueprint JSON file.

        Returns:
            API response from publish.

        Raises:
            FileNotFoundError: If the file does not exist.
            BlueprintRegistryError: On publish failure.
        """
        if not path.exists():
            raise FileNotFoundError(f"Blueprint file not found: {path}")

        data = json.loads(path.read_text(encoding="utf-8"))
        return self.publish_blueprint(data)

    def is_reachable(self) -> bool:
        """Check if the registry API is reachable.

        Returns:
            True if a basic request succeeds, False otherwise.
        """
        try:
            self._request("GET", "/blueprints")
            return True
        except BlueprintRegistryError:
            return False
