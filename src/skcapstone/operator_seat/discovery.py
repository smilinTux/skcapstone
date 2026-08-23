"""Manifest-driven Atlas discovery: the load-bearing G1 fix (OPS0.3).

Today the operator seat's world is two hardcoded module-level dicts
(``registration.APP_REGISTRY`` and ``loop.ADAPTERS``); a new capability can only
reach Atlas by editing skcapstone source and shipping a release. That is the
CRITICAL G1 finding of the 2026-07-31 ops-pack spec (section 1.3). This module is
the loader that removes it: a signed capability-pack manifest dropped into the
node's shell registry dir auto-registers with the operator seat, ALONGSIDE the
built-in seven, with no code change.

What it does, additively and fail-safe:

  * SCAN ``$SKCAPSTONE_HOME/shell/modules/*.skworld-module.json`` (the exact dir
    the shell aggregator already reads).
  * VERIFY each manifest's capauth signature through the operator-approved
    registry (``capauth.manifest.list_registered`` re-verifies each entry's
    detached signature over the manifest's current canonical bytes, pinned to
    ``SKCHAT_SHELL_SIGNER_FPR``, the chef root). An unsigned or unverified
    manifest is NEVER loaded: it is logged ``ManifestUnverified`` and skipped.
    This is the exact signing chain shipped this session (skchat.shell_modules).
  * BUILD an Operatorapp spec for each verified, non-built-in manifest through the
    PURE seam ``manifest_adapter.operatorapp_from_manifest`` and hand it to
    ``registration.register_all`` so it registers identically to the built-ins. A
    manifest whose id matches a built-in does NOT override it (builtins win).
  * RUN a discovered app OUT-OF-PROCESS through its declared
    ``<cli> operator observe --json`` contract (subprocess, JSON, hard timeout).
    ANY failure at that process boundary (nonzero exit, timeout, bad JSON,
    malformed payload) yields every declared condition as ``Unknown``: a
    fail-safe, never a crash and never an action.
  * PROBE the ``knowledge`` facet's declared retriever and note RAG availability
    (``SKOPERATOR_SKBRAIN=off|on|auto``, default auto) WITHOUT hard-depending on
    it: an absent retriever simply leaves RAG unavailable.

Everything is gated behind ``SKOPERATOR_MANIFEST_DISCOVERY`` (default OFF), so the
live seat behaves byte-identically until Chef flips it. Discovery only WIDENS what
Atlas observes and proposes: it introduces no auto-ratification and no
auto-execution. Ratification stays human-only by the store's existing guard, the
freeze primitive still gates everything, and the constitution/policy modules are
never manifest-readable surfaces.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import adapter, manifest_adapter

logger = logging.getLogger(__name__)

#: Master gate. Default OFF (unset): the seat behaves byte-identically to today.
#: Truthy set (``1``/``true``/``yes``/``on``, case-insensitive) turns discovery on.
DISCOVERY_ENV = "SKOPERATOR_MANIFEST_DISCOVERY"

#: Pin the manifest signer to a specific fingerprint/uid (the chef root). Reuses
#: the exact env the skchat shell aggregator uses so one flip governs both. Unset
#: accepts any cryptographically valid signature the operator registered.
SIGNER_FPR_ENV = "SKCHAT_SHELL_SIGNER_FPR"

#: RAG enrichment mode for the knowledge facet: ``off`` | ``on`` | ``auto``.
#: Default ``auto`` (the probe decides). ``off`` disables it; ``on`` assumes the
#: retriever is present without probing.
SKBRAIN_ENV = "SKOPERATOR_SKBRAIN"

#: Hard timeout (seconds) for a single out-of-process operator verb. A dead or
#: hung adapter can waste at most this long, then fails safe to Unknown.
SUBPROCESS_TIMEOUT = 10.0

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def discovery_enabled() -> bool:
    """Whether ``SKOPERATOR_MANIFEST_DISCOVERY`` is set to a truthy value (default OFF)."""
    return os.environ.get(DISCOVERY_ENV, "").strip().lower() in _TRUTHY


def skbrain_mode() -> str:
    """The RAG mode from ``SKOPERATOR_SKBRAIN``: ``off`` | ``on`` | ``auto`` (default auto)."""
    mode = os.environ.get(SKBRAIN_ENV, "auto").strip().lower()
    return mode if mode in {"off", "on", "auto"} else "auto"


def _signer_fpr() -> str | None:
    """The pinned manifest signer fingerprint/uid, or None to accept any valid one."""
    return os.environ.get(SIGNER_FPR_ENV, "").strip() or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _skcapstone_home(home: Path | str | None = None) -> Path:
    """Resolve the skcapstone home holding the shell registry + modules dir.

    Honors an explicit ``home``, then ``$SKCAPSTONE_HOME``, then ``~/.skcapstone``,
    mirroring ``capauth.manifest.shell_home`` so the dir we scan and the registry
    capauth verifies always agree.
    """
    if home is not None:
        return Path(home).expanduser()
    env = os.environ.get("SKCAPSTONE_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".skcapstone"


def _shell_modules_dir(home: Path | str | None = None) -> Path:
    """The node's shell registry dir: ``<home>/shell/modules/``."""
    return _skcapstone_home(home) / "shell" / "modules"


# --- capauth signature gate --------------------------------------------------


def _verified_module_ids(home: Path | str | None = None) -> set[str] | None:
    """Return the module ids the operator registry marks signed + enabled.

    Consults the operator-approved capauth registry
    (``<home>/shell/modules.json``) via ``capauth.manifest.list_registered``,
    which re-verifies each entry's detached signature over the manifest's current
    canonical bytes, pinned to :data:`SIGNER_FPR_ENV` when set. Only entries whose
    live verdict is ``ok`` AND whose operator ``enabled`` flag is true are
    returned; every other entry is logged ``ManifestUnverified`` and dropped.

    Returns:
        The set of verified + enabled module ids, or ``None`` when capauth is
        unavailable or the registry cannot be read. ``None`` signals the caller to
        FAIL CLOSED (discover nothing), never to trust everything.
    """
    try:
        from capauth.manifest import list_registered
    except Exception as exc:  # noqa: BLE001 - capauth optional; the gate fails closed
        logger.warning(
            "operator discovery: capauth unavailable (%s); failing closed (no discovery)", exc
        )
        return None

    signer = _signer_fpr()
    try:
        entries = list_registered(_skcapstone_home(home), expected_signer=signer)
    except Exception as exc:  # noqa: BLE001 - a bad registry must never crash discovery
        logger.warning("operator discovery: shell registry unreadable (%s); failing closed", exc)
        return None

    verified: set[str] = set()
    for entry in entries:
        mid = entry.get("id")
        if not mid:
            continue
        if entry.get("signature") == "ok" and entry.get("enabled", True):
            verified.add(mid)
        else:
            logger.info(
                "operator discovery: ManifestUnverified %r (signature=%s enabled=%s); skipping",
                mid,
                entry.get("signature"),
                entry.get("enabled"),
            )
    return verified


# --- out-of-process observe boundary -----------------------------------------


def _run_operator_json(cli: str, verb: str, *extra: str, timeout: float) -> dict | None:
    """Run ``<cli> <verb> [extra...] --json`` and parse stdout as a JSON object.

    ``cli`` is the manifest's ``operator.cli`` prefix (e.g. ``"skbrain operator"``),
    split on whitespace and suffixed with the verb, so the invocation is
    ``skbrain operator <verb> --json``.

    Returns:
        The parsed dict on a clean, zero-exit, JSON-object result, else ``None``
        on ANY failure (missing binary, nonzero exit, timeout, non-JSON, non-dict
        body). Never raises: the caller treats ``None`` as the fail-safe.
    """
    try:
        argv = shlex.split(cli) + [verb, *extra, "--json"]
    except ValueError as exc:
        logger.warning("operator discovery: unparseable cli %r (%s)", cli, exc)
        return None
    if not argv:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - argv from an operator-signed manifest
            argv, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("operator discovery: %s %s failed (%s)", cli, verb, exc)
        return None
    if proc.returncode != 0:
        logger.warning(
            "operator discovery: %s %s exited %s (%s)",
            cli,
            verb,
            proc.returncode,
            (proc.stderr or "").strip()[:200],
        )
        return None
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        logger.warning("operator discovery: %s %s emitted non-JSON (%s)", cli, verb, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("operator discovery: %s %s emitted a non-object payload", cli, verb)
        return None
    return data


def _unknown_conditions(conditions: list[str], now_iso: str) -> list[dict]:
    """Every declared condition as a fail-safe ``Unknown`` status dict."""
    return [
        {
            "type": cond,
            "status": "Unknown",
            "reason": "AdapterUnreachable",
            "message": "out-of-process operator adapter failed; failing safe to Unknown",
            "lastTransition": now_iso,
        }
        for cond in conditions
    ]


def _skbrain_is_healthy(
    cli: str,
    conditions: list[str],
    *,
    timeout: float,
    runner: Callable[..., dict | None] | None,
) -> bool:
    """Return whether SKBrain is healthy enough to expose to Atlas.

    SKBrain is a knowledge/control-plane dependency, not a decorative module.
    A valid signature proves provenance but does not prove that its schema,
    projector, or KEDB are usable.  Require one conformant observation in which
    every declared condition is ``True`` before registration.  Missing
    executables, timeouts, malformed responses, partial condition sets, and
    False/Unknown conditions all fail closed.
    """
    run = runner or _run_operator_json
    payload = run(cli, "observe", timeout=timeout)
    if not isinstance(payload, dict) or adapter.validate_observe(payload):
        return False
    states = {
        item.get("type"): item.get("status")
        for item in payload.get("conditions", [])
        if isinstance(item, dict)
    }
    return bool(conditions) and all(states.get(condition) == "True" for condition in conditions)


def make_subprocess_observe(
    cli: str,
    conditions: list[str],
    *,
    timeout: float = SUBPROCESS_TIMEOUT,
    runner: Callable[..., dict | None] | None = None,
) -> Callable[..., dict]:
    """Build a loop-compatible ``observe(paths, now_iso)`` over the OOP contract.

    The returned callable runs ``<cli> operator observe --json`` out-of-process and
    returns its ``{conditions: [...]}`` payload when it is present AND conformant
    (``adapter.validate_observe`` clean). On ANY failure at the process boundary,
    or a malformed payload, it returns every declared condition as ``Unknown``:
    the fail-safe contract applied at the subprocess boundary. It never raises and
    never invokes an ``act`` verb.

    Args:
        cli: The manifest's ``operator.cli`` prefix (e.g. ``"skbrain operator"``).
        conditions: The condition names the manifest declares (the Unknown floor).
        timeout: Hard per-call timeout in seconds.
        runner: Injectable process runner (tests supply a fake); defaults to
            :func:`_run_operator_json`.

    Returns:
        ``observe(paths=None, now_iso=None) -> {"conditions": [...]}``.
    """
    run = runner or _run_operator_json
    declared = list(conditions)

    def observe(paths: Any = None, now_iso: str | None = None, **_: Any) -> dict:
        now = now_iso or _now_iso()
        payload = run(cli, "observe", timeout=timeout)
        if isinstance(payload, dict) and not adapter.validate_observe(payload):
            return {"conditions": list(payload.get("conditions", []))}
        return {"conditions": _unknown_conditions(declared, now)}

    return observe


# --- knowledge-facet RAG probe -----------------------------------------------


def _default_knowledge_prober(knowledge: manifest_adapter.KnowledgeSource) -> bool:
    """Best-effort probe: is the declared retriever factory importable?

    A cheap, dependency-free probe for the ``knowledge`` facet's declared
    ``module:callable`` retriever ref. Returns True only when the module imports
    and the attribute exists. Never raises: a missing retriever (the common case
    until the ops read-API lands) simply reads as RAG-unavailable, so the seat
    never hard-depends on it. The full DB-schema probe (reader-role connect +
    ``information_schema`` check) is OPS2.2's refinement onto this hook.
    """
    ref = knowledge.retriever
    if not ref or ":" not in ref:
        return False
    mod_name, _, attr = ref.partition(":")
    try:
        import importlib

        mod = importlib.import_module(mod_name)
    except Exception:  # noqa: BLE001 - retriever absent -> RAG unavailable, fail-safe
        return False
    return bool(attr) and hasattr(mod, attr)


def probe_knowledge(
    knowledge: manifest_adapter.KnowledgeSource | None,
    *,
    prober: Callable[[manifest_adapter.KnowledgeSource], bool] | None = None,
) -> bool:
    """Whether RAG enrichment is available for a knowledge facet, honoring the mode.

    Reads :data:`SKBRAIN_ENV` (``off`` | ``on`` | ``auto``, default auto):

    * ``off``    -> always False (RAG disabled);
    * ``on``     -> always True when a facet is declared (assume present, no probe);
    * ``auto``   -> the ``prober`` decides (defaults to :func:`_default_knowledge_prober`).

    Never raises: a probe error reads as RAG-unavailable.
    """
    if knowledge is None:
        return False
    mode = skbrain_mode()
    if mode == "off":
        return False
    if mode == "on":
        return True
    run = prober or _default_knowledge_prober
    try:
        return bool(run(knowledge))
    except Exception as exc:  # noqa: BLE001 - probe failure -> RAG unavailable, fail-safe
        logger.info("operator discovery: knowledge probe failed (%s); RAG unavailable", exc)
        return False


# --- discovered app model + the discovery pass -------------------------------


@dataclass(frozen=True)
class DiscoveredApp:
    """One verified, non-built-in operator app discovered from a signed manifest.

    Attributes:
        name: The manifest id (the Operatorapp name).
        spec: The normalized Operatorapp spec (from
            ``manifest_adapter.operatorapp_from_manifest``), ready for
            ``registration.register_all`` alongside the built-ins.
        cli: The ``operator.cli`` prefix used for the out-of-process contract.
        observe: A loop-compatible ``observe(paths, now_iso)`` running
            ``<cli> operator observe --json``, fail-safe Unknown on any error.
        knowledge: The knowledge-source descriptor, or None when none is declared.
        rag_available: Whether the knowledge facet's retriever probed present.
    """

    name: str
    spec: dict
    cli: str
    observe: Callable[..., dict]
    knowledge: manifest_adapter.KnowledgeSource | None
    rag_available: bool


def builtin_app_ids() -> frozenset[str]:
    """The ids Atlas already operates in-process (never overridden by a manifest).

    The union of the registration registry (``APP_REGISTRY``) and the loop's
    observe adapters (``ADAPTERS``): a discovered manifest whose id is in this set
    is skipped so the built-ins keep precedence, cheaply and without a subprocess.
    Imported lazily to avoid an import cycle.
    """
    from . import loop, registration

    return frozenset(registration.APP_REGISTRY) | frozenset(loop.ADAPTERS)


def discover_apps(
    *,
    builtin_ids: frozenset[str] | set[str] | None = None,
    home: Path | str | None = None,
    timeout: float = SUBPROCESS_TIMEOUT,
    runner: Callable[..., dict | None] | None = None,
    verified_ids_fn: Callable[..., set[str] | None] | None = None,
    knowledge_prober: Callable[[manifest_adapter.KnowledgeSource], bool] | None = None,
) -> list[DiscoveredApp]:
    """Discover verified, non-built-in operator apps from signed shell manifests.

    Returns an EMPTY list when discovery is gated off, when capauth/registry is
    unavailable (fail closed), or when anything goes wrong wholesale, so the caller
    can always union the result with the built-ins safely.

    Args:
        builtin_ids: The ids to never override; defaults to :func:`builtin_app_ids`.
        home: skcapstone home override (the dir + registry root).
        timeout: Hard per-call timeout for the out-of-process observe.
        runner: Injectable process runner for the observe adapters (tests).
        verified_ids_fn: Injectable signature gate (tests); defaults to
            :func:`_verified_module_ids`.
        knowledge_prober: Injectable knowledge probe (tests).

    Returns:
        The discovered apps, in manifest-id order.
    """
    if not discovery_enabled():
        return []
    try:
        return _discover(
            builtin_ids=builtin_ids if builtin_ids is not None else builtin_app_ids(),
            home=home,
            timeout=timeout,
            runner=runner,
            verified_ids_fn=verified_ids_fn or _verified_module_ids,
            knowledge_prober=knowledge_prober,
        )
    except Exception as exc:  # noqa: BLE001 - discovery must never break the seat
        logger.warning("operator discovery failed wholesale (%s); no discovered apps", exc)
        return []


def _discover(
    *,
    builtin_ids: frozenset[str] | set[str],
    home: Path | str | None,
    timeout: float,
    runner: Callable[..., dict | None] | None,
    verified_ids_fn: Callable[..., set[str] | None],
    knowledge_prober: Callable[[manifest_adapter.KnowledgeSource], bool] | None,
) -> list[DiscoveredApp]:
    verified = verified_ids_fn(home)
    if verified is None:
        # capauth/registry unavailable: fail closed, discover nothing.
        return []

    modules_dir = _shell_modules_dir(home)
    try:
        files = sorted(modules_dir.glob("*.skworld-module.json"))
    except Exception as exc:  # noqa: BLE001 - missing/unreadable dir is fine, discover none
        logger.info("operator discovery: no modules dir (%s)", exc)
        return []

    apps: list[DiscoveredApp] = []
    for path in files:
        app = _discover_one(
            path,
            builtin_ids=builtin_ids,
            verified=verified,
            timeout=timeout,
            runner=runner,
            knowledge_prober=knowledge_prober,
        )
        if app is not None:
            apps.append(app)
    return apps


def _discover_one(
    path: Path,
    *,
    builtin_ids: frozenset[str] | set[str],
    verified: set[str],
    timeout: float,
    runner: Callable[..., dict | None] | None,
    knowledge_prober: Callable[[manifest_adapter.KnowledgeSource], bool] | None,
) -> DiscoveredApp | None:
    """Turn one manifest file into a DiscoveredApp, or None (logged) if unusable."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad file skips, the rest still discover
        logger.info("operator discovery: skipping unreadable %s (%s)", path, exc)
        return None
    if not isinstance(manifest, dict):
        logger.info("operator discovery: skipping %s (not a JSON object)", path)
        return None

    mid = manifest.get("id")
    if not isinstance(mid, str) or not mid:
        logger.info("operator discovery: skipping %s (no id)", path)
        return None

    if mid in builtin_ids:
        # A manifest whose id matches a built-in does NOT override it; the
        # in-process adapter keeps precedence (cheap, no subprocess).
        logger.debug("operator discovery: %r is a built-in; keeping the in-process adapter", mid)
        return None

    if mid not in verified:
        logger.warning("operator discovery: ManifestUnverified %r at %s; NOT loaded", mid, path)
        return None

    errors = manifest_adapter.validate_manifest(manifest)
    if errors:
        logger.warning(
            "operator discovery: %r manifest invalid (%s); skipping",
            mid,
            "; ".join(f"{e.field}: {e.message}" for e in errors[:3]),
        )
        return None

    try:
        spec = manifest_adapter.operatorapp_from_manifest(manifest)
    except Exception as exc:  # noqa: BLE001 - malformed operator facet skips the app
        logger.warning("operator discovery: %r operator facet unusable (%s); skipping", mid, exc)
        return None

    cli = (manifest.get("operator") or {}).get("cli")
    if not isinstance(cli, str) or not cli:
        logger.warning(
            "operator discovery: %r declares no operator.cli; cannot run out-of-process, skipping",
            mid,
        )
        return None

    # A signed but absent/unhealthy SKBrain must not be advertised in the
    # Operatorapp registry or UI.  Other apps retain Unknown-condition exposure;
    # this strict gate is specific to the optional knowledge plane.
    if mid == "skbrain" and not _skbrain_is_healthy(
        cli, spec["conditions"], timeout=timeout, runner=runner
    ):
        logger.warning(
            "operator discovery: %r failed its complete health gate; not exposing it", mid
        )
        return None

    try:
        knowledge = manifest_adapter.knowledge_source_from_manifest(manifest)
    except Exception as exc:  # noqa: BLE001 - a bad knowledge facet just disables RAG
        logger.info("operator discovery: %r knowledge facet unusable (%s)", mid, exc)
        knowledge = None

    observe = make_subprocess_observe(cli, spec["conditions"], timeout=timeout, runner=runner)
    rag = probe_knowledge(knowledge, prober=knowledge_prober)
    if knowledge is not None:
        logger.info(
            "operator discovery: %r knowledge facet declared; RAG %s",
            mid,
            "available" if rag else "unavailable",
        )
    return DiscoveredApp(
        name=mid,
        spec=spec,
        cli=cli,
        observe=observe,
        knowledge=knowledge,
        rag_available=rag,
    )


# --- convenience wiring for bootstrap + the loop -----------------------------


def discover_operatorapp_specs(*, home: Path | str | None = None, **kwargs: Any) -> list[dict]:
    """The normalized Operatorapp specs for discovered apps (for ``register_all``)."""
    return [app.spec for app in discover_apps(home=home, **kwargs)]


def discover_observers(
    *, home: Path | str | None = None, **kwargs: Any
) -> dict[str, Callable[..., dict]]:
    """Discovered apps as ``{name: observe}`` for the loop (empty when gated off)."""
    return {app.name: app.observe for app in discover_apps(home=home, **kwargs)}


__all__ = [
    "DISCOVERY_ENV",
    "SIGNER_FPR_ENV",
    "SKBRAIN_ENV",
    "SUBPROCESS_TIMEOUT",
    "DiscoveredApp",
    "discovery_enabled",
    "skbrain_mode",
    "builtin_app_ids",
    "make_subprocess_observe",
    "probe_knowledge",
    "discover_apps",
    "discover_operatorapp_specs",
    "discover_observers",
]
