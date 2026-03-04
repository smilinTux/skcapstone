"""
SKCapstone Python Client SDK.

Async HTTP client for all ``/api/v1/*`` REST endpoints exposed by the
SKCapstone daemon (``skcapstone[api]``).

Install
-------
.. code-block:: bash

    pip install "skcapstone[client]"

Usage
-----
.. code-block:: python

    import asyncio
    from skcapstone.client import SKCapstoneClient

    async def main() -> None:
        async with SKCapstoneClient("http://127.0.0.1:7779", token="sk-...") as client:
            # Lightweight liveness check
            pong = await client.ping()
            print(pong)  # {"pong": True, "pid": 12345}

            # Full daemon health snapshot
            health = await client.health()
            print(health["status"])  # "ok"

            # Dashboard & capstone pillars
            dash = await client.dashboard()
            cap  = await client.capstone()

            # Household agents
            agents = await client.household_agents()
            opus   = await client.household_agent("opus")

            # Conversations
            threads = await client.conversations()
            history = await client.conversation("jarvis")
            sent    = await client.send_message("jarvis", "Hello from the client SDK!")
            deleted = await client.delete_conversation("jarvis")

            # Runtime metrics & component health
            m = await client.metrics()
            c = await client.components()

    asyncio.run(main())

Notes
-----
* The ``/api/v1/activity`` endpoint returns a Server-Sent Events (SSE) stream.
  Use ``httpx-sse`` or ``aiohttp`` directly for that endpoint; it is not wrapped
  here because ``httpx.AsyncClient`` does not natively parse SSE frames.

* The ``/api/v1/logs`` endpoint is a WebSocket.  Use the ``websockets`` library
  or ``httpx-ws`` to connect:

  .. code-block:: python

      import websockets
      async with websockets.connect(
          "ws://127.0.0.1:7779/api/v1/logs",
          extra_headers={"Authorization": f"Bearer {token}"},
      ) as ws:
          async for msg in ws:
              print(msg)
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:
    import httpx
except ImportError as _exc:
    raise ImportError(
        "httpx is required for SKCapstoneClient.  "
        "Install with: pip install 'skcapstone[client]'"
    ) from _exc


class SKCapstoneClient:
    """Async HTTP client for the SKCapstone daemon REST API.

    All methods are coroutines and return plain ``dict`` / ``list`` objects
    decoded from the JSON response body.  ``httpx.HTTPStatusError`` is raised
    for 4xx / 5xx responses.

    Parameters
    ----------
    base_url:
        Root URL of the running daemon, e.g. ``"http://127.0.0.1:7779"``.
    token:
        API key (``X-API-Key`` header) **or** CapAuth Bearer token
        (``Authorization: Bearer <token>`` header).  Pass a plain API key
        string for the standard key-based auth; pass a CapAuth PGP token
        the same way — the client always sends both headers so the server
        can pick the appropriate scheme.
    timeout:
        Default request timeout in seconds (default: 30.0).

    Examples
    --------
    .. code-block:: python

        # Context-manager style (recommended)
        async with SKCapstoneClient("http://127.0.0.1:7779", token="sk-abc") as c:
            print(await c.health())

        # Manual lifecycle
        client = SKCapstoneClient("http://127.0.0.1:7779", token="sk-abc")
        await client.aopen()
        try:
            print(await client.ping())
        finally:
            await client.aclose()
    """

    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def _build_headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {}
        if self._token:
            headers["X-API-Key"] = self._token
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def aopen(self) -> None:
        """Explicitly open the underlying ``httpx.AsyncClient``."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=self._build_headers(),
            timeout=self._timeout,
        )

    async def aclose(self) -> None:
        """Close the underlying ``httpx.AsyncClient`` and release connections."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "SKCapstoneClient":
        await self.aopen()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "SKCapstoneClient is not open.  "
                "Use 'async with SKCapstoneClient(...)' or call aopen() first."
            )
        return self._client

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._http().get(path, params={k: v for k, v in params.items() if v is not None})
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: Any) -> Any:
        resp = await self._http().post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> Any:
        resp = await self._http().delete(path)
        resp.raise_for_status()
        return resp.json()

    # ── Health ─────────────────────────────────────────────────────────────────

    async def ping(self) -> Dict[str, Any]:
        """Lightweight liveness check.

        Returns
        -------
        dict
            ``{"pong": true, "pid": <int>}``
        """
        return await self._get("/ping")

    async def health(self) -> Dict[str, Any]:
        """Comprehensive daemon health snapshot.

        Returns
        -------
        dict
            HealthResponse fields: ``status``, ``uptime_seconds``,
            ``daemon_pid``, ``consciousness_enabled``,
            ``self_healing_last_run``, ``self_healing_issues_found``,
            ``self_healing_auto_fixed``, ``backend_health``,
            ``disk_free_gb``, ``memory_usage_mb``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon is not running.
        """
        return await self._get("/api/v1/health")

    async def components(self) -> Dict[str, Any]:
        """Daemon subsystem component health.

        Returns
        -------
        dict
            ``{"components": [ComponentSnapshot, ...]}``
            Each snapshot has: ``name``, ``status``, ``last_heartbeat``,
            ``restart_count``, ``details``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get("/api/v1/components")

    # ── Dashboard ──────────────────────────────────────────────────────────────

    async def dashboard(self) -> Dict[str, Any]:
        """Full daemon dashboard snapshot.

        Returns
        -------
        dict
            DashboardResponse fields: ``agent``, ``daemon``,
            ``consciousness``, ``backends``, ``conversations``,
            ``system``, ``recent_errors``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get("/api/v1/dashboard")

    async def capstone(self) -> Dict[str, Any]:
        """Capstone pillars, memory stats, coordination board, and consciousness.

        Returns
        -------
        dict
            CapstoneResponse fields: ``agent``, ``pillars``, ``memory``,
            ``board``, ``consciousness``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get("/api/v1/capstone")

    # ── Metrics ────────────────────────────────────────────────────────────────

    async def metrics(self) -> Dict[str, Any]:
        """Consciousness loop runtime metrics.

        Returns
        -------
        dict
            MetricsResponse fields: ``loop_count``, ``messages_processed``,
            ``avg_loop_duration_ms``, ``error_count``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the consciousness loop is not loaded.
        """
        return await self._get("/api/v1/metrics")

    # ── Household ──────────────────────────────────────────────────────────────

    async def household_agents(self) -> Dict[str, Any]:
        """List all agents known to the shared household.

        Returns
        -------
        dict
            ``{"agents": [HouseholdAgent, ...]}``
            Each agent has: ``name``, ``status``, ``identity``,
            ``heartbeat``, ``consciousness``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get("/api/v1/household/agents")

    async def household_agent(self, name: str) -> Dict[str, Any]:
        """Get details for a specific household agent.

        Parameters
        ----------
        name:
            Agent directory name, e.g. ``"opus"``.

        Returns
        -------
        dict
            HouseholdAgent fields: ``name``, ``status``, ``identity``,
            ``heartbeat``, ``memory_count``, ``consciousness``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 404 when the agent is not found.
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get(f"/api/v1/household/agent/{name}")

    # ── Conversations ──────────────────────────────────────────────────────────

    async def conversations(self) -> Dict[str, Any]:
        """List all conversation threads, most recently active first.

        Returns
        -------
        dict
            ``{"conversations": [ConversationSummary, ...]}``
            Each summary has: ``peer``, ``message_count``,
            ``last_message_time``, ``last_message_preview``.

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get("/api/v1/conversations")

    async def conversation(self, peer: str) -> Dict[str, Any]:
        """Get full message history for a conversation with a peer.

        Parameters
        ----------
        peer:
            Peer agent or user name (alphanumeric, dashes, underscores).

        Returns
        -------
        dict
            ``{"peer": str, "messages": [msg, ...]}``

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 404 when no conversation exists for this peer.
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._get(f"/api/v1/conversations/{peer}")

    async def send_message(self, peer: str, content: str) -> Dict[str, Any]:
        """Send a message to a named peer.

        Parameters
        ----------
        peer:
            Target peer agent or user name.
        content:
            Message text to send (must be non-empty).

        Returns
        -------
        dict
            ``{"status": "sent", "message_id": "<uuid>"}``

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 400 when the peer name is invalid or content is empty.
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._post(f"/api/v1/conversations/{peer}/send", {"content": content})

    async def delete_conversation(self, peer: str) -> Dict[str, Any]:
        """Permanently delete the conversation history for a peer.

        Parameters
        ----------
        peer:
            Peer name whose conversation to delete.

        Returns
        -------
        dict
            ``{"status": "deleted", "peer": str}``

        Raises
        ------
        httpx.HTTPStatusError
            HTTP 404 when no conversation file exists for this peer.
            HTTP 503 when the daemon context is not initialised.
        """
        return await self._delete(f"/api/v1/conversations/{peer}")

    # ── Legacy (deprecated) ────────────────────────────────────────────────────

    async def legacy_status(self) -> Dict[str, Any]:
        """Return the legacy daemon status snapshot.

        .. deprecated::
            Use :meth:`health` or :meth:`dashboard` instead.
        """
        return await self._get("/status")

    async def legacy_consciousness(self) -> Dict[str, Any]:
        """Return raw consciousness loop statistics.

        .. deprecated::
            Use :meth:`capstone` instead.
        """
        return await self._get("/consciousness")
