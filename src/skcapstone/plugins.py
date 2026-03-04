"""SKCapstone plugin loader.

Plugins live in ``~/.skcapstone/plugins/*.py`` and are loaded at daemon and
MCP server startup.  Each plugin module must expose a ``register`` function::

    def register(mcp_server, app) -> None:
        ...

``mcp_server`` is the :class:`mcp.server.Server` instance when called from
the MCP server process, or ``None`` when called from the daemon process.

``app`` is the FastAPI :class:`~fastapi.FastAPI` instance when the docs
server is running, or ``None`` otherwise.

Plugins that add MCP tools call :func:`register_tool` inside ``register``::

    from skcapstone.plugins import register_tool
    from mcp.types import Tool, TextContent

    async def _handler(arguments: dict) -> list[TextContent]:
        return [TextContent(type="text", text="pong")]

    def register(mcp_server, app):
        register_tool(
            Tool(
                name="ping",
                description="Respond with pong.",
                inputSchema={"type": "object", "properties": {}},
            ),
            _handler,
        )

The ``_registry`` singleton is process-local — the daemon and the MCP server
each maintain independent state.  Hot-reload is triggered by sending SIGHUP to
the daemon PID and causes a fresh scan of ``~/.skcapstone/plugins/``.

Files whose names start with ``_`` are skipped (use them for shared helpers).
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from typing import Any, Callable, Coroutine

logger = logging.getLogger("skcapstone.plugins")

PLUGIN_DIR_NAME = "plugins"


class PluginRegistry:
    """Process-local registry of dynamically loaded plugin tools and handlers.

    Each process (daemon, MCP server) owns an independent instance via the
    module-level ``_registry`` singleton.
    """

    def __init__(self) -> None:
        # Use plain list/dict so we don't force-import mcp at module load time.
        self._tools: list = []
        self._handlers: dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
        # Canonical resolved path → loaded module, for hot-reload eviction.
        self._loaded: dict[str, types.ModuleType] = {}

    # ------------------------------------------------------------------
    # Registration — called by plugins via register_tool()
    # ------------------------------------------------------------------

    def register_tool(
        self,
        tool: Any,
        handler: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        """Add an MCP tool + async handler to the registry.

        Idempotent: re-registering the same tool name replaces the old entry.
        """
        self._tools = [t for t in self._tools if t.name != tool.name]
        self._tools.append(tool)
        self._handlers[tool.name] = handler
        logger.debug("Plugin registered tool: %s", tool.name)

    # ------------------------------------------------------------------
    # Query — called by mcp_server.py at list/dispatch time
    # ------------------------------------------------------------------

    def get_tools(self) -> list:
        """Return a snapshot of all plugin-registered Tool definitions."""
        return list(self._tools)

    def get_handlers(self) -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
        """Return a snapshot of all plugin-registered handler callables."""
        return dict(self._handlers)

    # ------------------------------------------------------------------
    # Internal loading helpers
    # ------------------------------------------------------------------

    def _load_one(self, plugin_path: Path, mcp_server: Any, app: Any) -> bool:
        """Load a single plugin file and invoke its ``register()`` function.

        Returns ``True`` on success, ``False`` on any error.
        """
        module_name = f"skcapstone._plugin_{plugin_path.stem}"
        # Evict any cached version so hot-reload always re-executes the file.
        sys.modules.pop(module_name, None)
        try:
            spec = importlib.util.spec_from_file_location(module_name, plugin_path)
            if spec is None or spec.loader is None:
                logger.warning("Plugin %s: cannot create module spec", plugin_path.name)
                return False

            mod = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = mod
            spec.loader.exec_module(mod)  # type: ignore[union-attr]

            if not hasattr(mod, "register"):
                logger.warning(
                    "Plugin %s: no register() function — skipping", plugin_path.name
                )
                sys.modules.pop(module_name, None)
                return False

            mod.register(mcp_server, app)
            self._loaded[str(plugin_path.resolve())] = mod
            logger.info("Plugin loaded: %s", plugin_path.name)
            return True

        except Exception as exc:
            logger.error(
                "Plugin %s failed to load: %s", plugin_path.name, exc, exc_info=True
            )
            sys.modules.pop(module_name, None)
            return False

    # ------------------------------------------------------------------
    # Public loading API
    # ------------------------------------------------------------------

    def scan_and_load(self, plugin_dir: Path, mcp_server: Any, app: Any) -> int:
        """Scan *plugin_dir* for ``*.py`` files and load each as a plugin.

        Files whose names start with ``_`` are skipped (private helpers).
        Returns the number of successfully loaded plugins.
        """
        if not plugin_dir.exists():
            logger.debug("Plugin directory not found: %s", plugin_dir)
            return 0

        count = 0
        for plugin_path in sorted(plugin_dir.glob("*.py")):
            if plugin_path.name.startswith("_"):
                continue
            if self._load_one(plugin_path, mcp_server, app):
                count += 1

        if count:
            logger.info("Loaded %d plugin(s) from %s", count, plugin_dir)
        else:
            logger.debug("No plugins found in %s", plugin_dir)
        return count

    def clear(self) -> None:
        """Unload all plugins and reset the registry."""
        for mod in self._loaded.values():
            sys.modules.pop(getattr(mod, "__name__", ""), None)
        self._tools.clear()
        self._handlers.clear()
        self._loaded.clear()
        logger.debug("Plugin registry cleared")

    def reload(self, plugin_dir: Path, mcp_server: Any, app: Any) -> int:
        """Clear and reload all plugins from *plugin_dir*.

        Called on SIGHUP for hot-reload.  Returns the number of plugins loaded.
        """
        logger.info("Plugin hot-reload — clearing %d plugin(s)", len(self._loaded))
        self.clear()
        return self.scan_and_load(plugin_dir, mcp_server, app)


# Module-level singleton — each process (daemon / MCP server) has its own copy.
_registry = PluginRegistry()


# ------------------------------------------------------------------
# Public convenience API used by plugins and the MCP server
# ------------------------------------------------------------------


def register_tool(
    tool: Any,
    handler: Callable[..., Coroutine[Any, Any, Any]],
) -> None:
    """Register an MCP tool from within a plugin's ``register()`` function.

    Example::

        from skcapstone.plugins import register_tool
        from mcp.types import Tool, TextContent

        async def _handler(arguments: dict) -> list[TextContent]:
            return [TextContent(type="text", text="ok")]

        def register(mcp_server, app):
            register_tool(
                Tool(
                    name="my_tool",
                    description="Does something useful.",
                    inputSchema={"type": "object", "properties": {}},
                ),
                _handler,
            )
    """
    _registry.register_tool(tool, handler)


def load_plugins(home: Path, mcp_server: Any, app: Any) -> int:
    """Scan ``{home}/plugins/*.py`` and load each plugin.

    Returns the count of successfully loaded plugins.
    """
    return _registry.scan_and_load(home / PLUGIN_DIR_NAME, mcp_server, app)


def reload_plugins(home: Path, mcp_server: Any, app: Any) -> int:
    """Clear and reload all plugins from ``{home}/plugins/*.py``.

    Intended to be called from the SIGHUP handler for live hot-reload.
    """
    return _registry.reload(home / PLUGIN_DIR_NAME, mcp_server, app)


def get_plugin_tools() -> list:
    """Return all MCP Tool definitions registered by plugins."""
    return _registry.get_tools()


def get_plugin_handlers() -> dict[str, Callable[..., Coroutine[Any, Any, Any]]]:
    """Return all handler callables registered by plugins."""
    return _registry.get_handlers()
