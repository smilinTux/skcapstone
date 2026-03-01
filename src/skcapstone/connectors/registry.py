"""
ConnectorRegistry — discover and instantiate platform connectors.

Maintains a catalogue of every known ConnectorBackend class and can
probe each one to report which platforms are currently reachable.

Built-in connectors are always registered.  Third-party connectors can
be registered at runtime via register().

Usage::

    registry = ConnectorRegistry()
    available = registry.available()          # only CONNECTED/DISCONNECTED
    connector = registry.create("terminal")   # instantiate by type name
    connector.connect()
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple, Type

from .base import ConnectorBackend, ConnectorInfo, ConnectorStatus, ConnectorType
from .cursor import CursorConnector
from .terminal import TerminalConnector
from .vscode import VSCodeConnector

logger = logging.getLogger(__name__)

# Built-in connector classes indexed by ConnectorType value
_BUILTIN_CONNECTORS: Dict[str, Type[ConnectorBackend]] = {
    ConnectorType.TERMINAL.value: TerminalConnector,
    ConnectorType.VSCODE.value: VSCodeConnector,
    ConnectorType.CURSOR.value: CursorConnector,
}


class ConnectorRegistry:
    """Catalogue of sovereign agent platform connectors.

    Probes each registered connector to determine which platforms are
    currently reachable without establishing a persistent connection.

    ``start_all()`` / ``stop_all()`` provide a simple lifecycle API: they
    instantiate every registered connector, call ``connect()`` / ``disconnect()``
    on each one, and keep the live instances in an internal table so that
    callers can retrieve them later.

    Args:
        auto_register_builtins: If True (default), pre-populate with all
            built-in connectors on construction.
    """

    def __init__(self, auto_register_builtins: bool = True) -> None:
        self._registry: Dict[str, Type[ConnectorBackend]] = {}
        self._running: Dict[str, ConnectorBackend] = {}
        if auto_register_builtins:
            self._registry.update(_BUILTIN_CONNECTORS)

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        connector_class: Type[ConnectorBackend],
    ) -> None:
        """Register a connector class under a given name.

        Overwrites any existing entry with the same name.

        Args:
            name: Lookup key (e.g. ConnectorType value or custom slug).
            connector_class: Class that implements ConnectorBackend.
        """
        self._registry[name] = connector_class
        logger.debug("Registered connector: %s -> %s", name, connector_class.__name__)

    def unregister(self, name: str) -> bool:
        """Remove a connector from the registry.

        Args:
            name: The key used when the connector was registered.

        Returns:
            True if the key existed and was removed, False otherwise.
        """
        if name in self._registry:
            del self._registry[name]
            return True
        return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_names(self) -> List[str]:
        """Return the names of all registered connectors.

        Returns:
            Sorted list of connector name strings.
        """
        return sorted(self._registry.keys())

    def probe(self) -> List[ConnectorInfo]:
        """Probe all registered connectors and return their status snapshots.

        Each probe instantiates the connector with default args and calls
        health_check() — no persistent connection is established.

        Returns:
            List of ConnectorInfo objects sorted by connector name.
        """
        results: List[ConnectorInfo] = []
        for name, cls in sorted(self._registry.items()):
            try:
                instance = cls()
                status = instance.health_check()
                results.append(
                    ConnectorInfo(
                        name=name,
                        connector_type=instance.connector_type,
                        status=status,
                    )
                )
            except Exception as exc:
                logger.warning("probe: error checking %s: %s", name, exc)
                results.append(
                    ConnectorInfo(
                        name=name,
                        connector_type=ConnectorType.UNKNOWN,
                        status=ConnectorStatus.ERROR,
                        metadata={"error": str(exc)},
                    )
                )
        return results

    def available(self) -> List[ConnectorInfo]:
        """Return only connectors that are not UNAVAILABLE or in ERROR.

        Returns:
            Subset of probe() results where status is CONNECTED or DISCONNECTED.
        """
        return [
            info
            for info in self.probe()
            if info.status not in (ConnectorStatus.UNAVAILABLE, ConnectorStatus.ERROR)
        ]

    # ------------------------------------------------------------------
    # Instantiation
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        **kwargs: object,
    ) -> Optional[ConnectorBackend]:
        """Instantiate a connector by its registered name.

        Args:
            name: Connector name as returned by list_names().
            **kwargs: Forwarded to the connector's __init__.

        Returns:
            New ConnectorBackend instance, or None if name is not registered.
        """
        cls = self._registry.get(name)
        if cls is None:
            logger.warning("ConnectorRegistry.create: unknown connector %r", name)
            return None
        return cls(**kwargs)

    def get_class(self, name: str) -> Optional[Type[ConnectorBackend]]:
        """Return the connector class registered under name, without instantiating.

        Args:
            name: Connector name.

        Returns:
            ConnectorBackend subclass, or None if not found.
        """
        return self._registry.get(name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start_all(self, **kwargs: object) -> List[Tuple[str, bool]]:
        """Instantiate and connect every registered connector.

        Already-running connectors (present in the internal running table)
        are skipped — call ``stop_all()`` first to restart them.

        Args:
            **kwargs: Forwarded to each connector's ``__init__`` where accepted.
                Most built-in connectors take no extra arguments, so omit this
                in normal usage.

        Returns:
            List of ``(name, success)`` tuples in registry order, where
            ``success`` is the return value of ``connect()``.
        """
        results: List[Tuple[str, bool]] = []
        for name, cls in sorted(self._registry.items()):
            if name in self._running:
                logger.debug("start_all: %s is already running — skipping", name)
                results.append((name, True))
                continue
            try:
                instance = cls(**kwargs)
                ok = instance.connect()
                if ok:
                    self._running[name] = instance
                    logger.info("start_all: %s connected", name)
                else:
                    logger.warning("start_all: %s connect() returned False", name)
                results.append((name, ok))
            except Exception as exc:
                logger.error("start_all: %s raised %s", name, exc)
                results.append((name, False))
        return results

    def stop_all(self) -> List[Tuple[str, bool]]:
        """Disconnect and remove every running connector instance.

        Returns:
            List of ``(name, success)`` tuples where ``success`` is the
            return value of ``disconnect()``.
        """
        results: List[Tuple[str, bool]] = []
        for name, instance in list(self._running.items()):
            try:
                ok = instance.disconnect()
                logger.info("stop_all: %s disconnected (ok=%s)", name, ok)
                results.append((name, ok))
            except Exception as exc:
                logger.error("stop_all: %s raised %s", name, exc)
                results.append((name, False))
            finally:
                self._running.pop(name, None)
        return results

    def get_running(self, name: str) -> Optional[ConnectorBackend]:
        """Return the live connector instance for *name*, if started.

        Args:
            name: Connector name as returned by ``list_names()``.

        Returns:
            Running ConnectorBackend instance, or None if not started.
        """
        return self._running.get(name)
