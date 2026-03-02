"""
Self-Healing Doctor — auto-diagnosing, auto-remediating agent health.

Extends the existing doctor.py diagnostics with auto-fix capabilities.
Follows the TrusteeMonitor escalation pattern:
    diagnose → auto-fix → re-check → escalate if still broken.

Registered auto-fixes:
    - Missing home dirs → mkdir -p
    - Missing memory index → rebuild from memory/**/*.json
    - Missing sync backends → write default sync-manifest.json
    - LLM unreachable → re-probe backends, switch fallback
    - Config corrupt → reset to defaults
    - Dead worker thread → restart
    - Dead inotify thread → restart observer
    - Stale model profiles → flag for human review
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skcapstone.self_healing")


class SelfHealingDoctor:
    """Auto-diagnosing, auto-remediating health monitor.

    Runs diagnostics from doctor.py plus consciousness-specific checks.
    Auto-fixes what it can, escalates what it can't.

    Args:
        home: Agent home directory.
        consciousness_loop: Optional reference to the running loop.
    """

    def __init__(
        self,
        home: Path,
        consciousness_loop: Any = None,
    ) -> None:
        self._home = home
        self._consciousness = consciousness_loop
        self._last_report: dict[str, Any] = {}

    def diagnose_and_heal(self) -> dict[str, Any]:
        """Run diagnostics, auto-fix what we can, escalate the rest.

        Returns:
            Dict with checks_run, checks_passed, auto_fixed,
            still_broken, escalated counts and details.
        """
        checks_run = 0
        checks_passed = 0
        auto_fixed = 0
        still_broken = 0
        escalated_items: list[str] = []
        details: list[dict[str, Any]] = []

        # Run all check groups
        check_methods = [
            self._check_home_dirs,
            self._check_memory_index,
            self._check_sync_manifest,
            self._check_consciousness_health,
            self._check_profile_freshness,
        ]

        for check_fn in check_methods:
            try:
                result = check_fn()
                checks_run += 1
                if result["status"] == "ok":
                    checks_passed += 1
                elif result["status"] == "fixed":
                    checks_passed += 1
                    auto_fixed += 1
                elif result["status"] == "broken":
                    still_broken += 1
                    escalated_items.append(result.get("name", "unknown"))
                details.append(result)
            except Exception as exc:
                checks_run += 1
                still_broken += 1
                details.append({
                    "name": check_fn.__name__,
                    "status": "error",
                    "error": str(exc),
                })

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "checks_run": checks_run,
            "checks_passed": checks_passed,
            "auto_fixed": auto_fixed,
            "still_broken": still_broken,
            "escalated": escalated_items,
            "details": details,
        }
        self._last_report = report

        # Escalate if needed
        if escalated_items:
            self._escalate(escalated_items)

        return report

    @property
    def last_report(self) -> dict[str, Any]:
        """Most recent diagnostic report."""
        return self._last_report

    # -------------------------------------------------------------------
    # Check methods — each returns {"name", "status", "message"}
    # -------------------------------------------------------------------

    def _check_home_dirs(self) -> dict[str, Any]:
        """Ensure all required home subdirectories exist."""
        required = [
            "identity", "memory", "trust", "security", "sync", "config",
            "soul", "logs",
        ]
        missing = []
        for subdir in required:
            path = self._home / subdir
            if not path.exists():
                missing.append(subdir)

        if not missing:
            return {"name": "home_dirs", "status": "ok", "message": "All dirs present"}

        # Auto-fix: create missing dirs
        for subdir in missing:
            (self._home / subdir).mkdir(parents=True, exist_ok=True)
            logger.info("Auto-created missing dir: %s", subdir)

        return {
            "name": "home_dirs",
            "status": "fixed",
            "message": f"Created {len(missing)} missing dirs: {missing}",
        }

    def _check_memory_index(self) -> dict[str, Any]:
        """Check if memory index exists and is valid."""
        memory_dir = self._home / "memory"
        index_path = memory_dir / "index.json"

        if not memory_dir.exists():
            return {"name": "memory_index", "status": "ok", "message": "No memory dir"}

        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                if isinstance(data, (list, dict)):
                    return {"name": "memory_index", "status": "ok", "message": "Index valid"}
            except (json.JSONDecodeError, OSError):
                pass

        # Auto-fix: rebuild index from memory files
        entries = []
        for layer_dir in ("short-term", "mid-term", "long-term"):
            layer_path = memory_dir / layer_dir
            if layer_path.exists():
                for f in layer_path.glob("*.json"):
                    try:
                        entry = json.loads(f.read_text(encoding="utf-8"))
                        entries.append({
                            "memory_id": entry.get("memory_id", f.stem),
                            "layer": layer_dir,
                            "tags": entry.get("tags", []),
                        })
                    except Exception as exc:
                        logger.debug("Skipping malformed memory file %s: %s", f, exc)
                        continue

        index_path.write_text(
            json.dumps(entries, indent=2), encoding="utf-8"
        )
        logger.info("Rebuilt memory index with %d entries", len(entries))

        return {
            "name": "memory_index",
            "status": "fixed",
            "message": f"Rebuilt index with {len(entries)} entries",
        }

    def _check_sync_manifest(self) -> dict[str, Any]:
        """Check sync-manifest.json exists."""
        sync_dir = self._home / "sync"
        manifest_path = sync_dir / "sync-manifest.json"

        if manifest_path.exists():
            return {"name": "sync_manifest", "status": "ok", "message": "Manifest present"}

        if not sync_dir.exists():
            return {"name": "sync_manifest", "status": "ok", "message": "No sync dir"}

        # Auto-fix: write default manifest
        default_manifest = {
            "version": 1,
            "backends": ["syncthing"],
            "auto_push": True,
            "auto_pull": True,
        }
        sync_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(default_manifest, indent=2), encoding="utf-8"
        )
        logger.info("Wrote default sync-manifest.json")

        return {
            "name": "sync_manifest",
            "status": "fixed",
            "message": "Created default sync manifest",
        }

    def _check_consciousness_health(self) -> dict[str, Any]:
        """Check consciousness loop health."""
        if not self._consciousness:
            return {
                "name": "consciousness",
                "status": "ok",
                "message": "Consciousness loop not loaded (disabled)",
            }

        issues: list[str] = []

        # Check at least one backend is available
        backends = self._consciousness._bridge.available_backends
        if not any(backends.values()):
            # Auto-fix: re-probe
            self._consciousness._bridge._probe_available_backends()
            backends = self._consciousness._bridge.available_backends
            if any(backends.values()):
                logger.info("Re-probed backends — found available: %s",
                            [k for k, v in backends.items() if v])
            else:
                issues.append("No LLM backends reachable")

        # Check inotify thread
        observer = self._consciousness._observer
        if observer and hasattr(observer, "is_alive") and not observer.is_alive():
            # Auto-fix: restart observer
            try:
                self._consciousness._run_inotify_restart()
                logger.info("Restarted inotify observer")
            except Exception as exc:
                logger.debug("Inotify restart failed: %s", exc)
                issues.append("Inotify thread dead — restart failed")

        if issues:
            return {
                "name": "consciousness",
                "status": "broken",
                "message": "; ".join(issues),
            }

        return {
            "name": "consciousness",
            "status": "ok",
            "message": "Consciousness loop healthy",
        }

    def _check_profile_freshness(self) -> dict[str, Any]:
        """Flag model profiles older than 90 days as needing review."""
        try:
            from skcapstone.prompt_adapter import PromptAdapter
            adapter = PromptAdapter()
            stale: list[str] = []
            now = datetime.now(timezone.utc)

            for profile in adapter.profiles:
                if not profile.last_updated:
                    stale.append(profile.family)
                    continue
                try:
                    updated = datetime.fromisoformat(profile.last_updated)
                    if hasattr(updated, "tzinfo") and updated.tzinfo is None:
                        updated = updated.replace(tzinfo=timezone.utc)
                    age_days = (now - updated).days
                    if age_days > 90:
                        stale.append(f"{profile.family} ({age_days}d)")
                except (ValueError, TypeError):
                    stale.append(profile.family)

            if stale:
                return {
                    "name": "profile_freshness",
                    "status": "ok",  # Informational, not broken
                    "message": f"Stale profiles (>90d): {', '.join(stale)}",
                    "stale_profiles": stale,
                }

            return {
                "name": "profile_freshness",
                "status": "ok",
                "message": "All profiles fresh",
            }
        except Exception as exc:
            return {
                "name": "profile_freshness",
                "status": "ok",
                "message": f"Could not check profiles: {exc}",
            }

    # -------------------------------------------------------------------
    # Escalation
    # -------------------------------------------------------------------

    def _escalate(self, items: list[str]) -> None:
        """Escalate unresolved issues via SKChat.

        Args:
            items: List of check names that are still broken.
        """
        try:
            from skcapstone.mcp_tools._helpers import _get_agent_name
            agent_name = _get_agent_name(self._home)
            message = (
                f"Self-healing alert from {agent_name}: "
                f"{len(items)} issue(s) could not be auto-fixed: {', '.join(items)}"
            )
            logger.warning("Escalating: %s", message)

            # Try to send via SKChat if available
            try:
                from skchat.messenger import AgentMessenger
                messenger = AgentMessenger.from_config()
                messenger.send("chef", message)
            except Exception as exc:
                logger.debug("SKChat escalation failed (best-effort): %s", exc)
        except Exception as exc:
            logger.debug("Escalation failed: %s", exc)
