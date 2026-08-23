"""Pure builders that turn a SKWorld module manifest into operator-seat structures.

This is the seam that lets Atlas discovery become MANIFEST-DRIVEN instead of the
two hardcoded module-level dicts it is today (``registration.APP_REGISTRY`` and
``loop.OBSERVERS``, the CRITICAL G1 finding of the 2026-07-31 ops-pack spec). A
signed ``skworld.module.json`` (schema v1.2, with the ratified ``operator`` facet
plus the optional ``install`` and ``knowledge`` facets) is parsed here into:

  * an Operatorapp spec (mirrors ``registration.derive_operatorapp_spec`` output,
    sourced from the ``operator`` facet), so a discovered app registers exactly
    the way the built-in seven do today;
  * an ``InstallPlan`` of typed, ordered step descriptors from the ``install``
    facet (a PURE description of what an installer would do, never execution);
  * a ``KnowledgeSource`` retriever descriptor from the ``knowledge`` facet; and
  * a structured validation of the manifest against the v1.2 contract.

Everything here is PURE: no filesystem, no database, no gpg, no network. The
capauth signature is NOT verified here (that is the discovery-wiring card OPS0.3
and the skos provisioner OPS1.x); this module only MODELS the signed-manifest
contract, so a caller can require that the signature envelope is present before it
trusts a manifest. OPS0.3 (seat discovery) and OPS1.x (skos install planner) both
consume the structures built here; keeping this layer side-effect free is what
lets both wire it without inheriting each other's I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from ..fleet import operatorapp

#: The install-facet step kinds this adapter understands (spec 2.3 / 2.4). Each
#: maps to one provisioner step kind in skos; unknown kinds are a validation error.
STEP_KINDS: frozenset[str] = frozenset(
    {
        "sql_migration",
        "db_roles",
        "content_repo",
        "seed",
        "fleet_objects",
        "doctor",
    }
)

#: The fields each step kind must carry to be a well-formed descriptor. Optional
#: fields (pre_dump, verify, password_source, defer_ok, private, ...) are preserved
#: verbatim but not required here; the provisioner (OPS1.x) owns their semantics.
_STEP_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "sql_migration": ("db", "script"),
    "db_roles": ("logins",),
    "content_repo": ("name", "dest"),
    "seed": ("cmd",),
    "fleet_objects": ("objects",),
    "doctor": ("checks",),
}

#: Manifest keys that may carry the schema version (the shipped builders emit
#: ``schemaVersion``; the spec sketch writes ``schema_version``: accept both).
_SCHEMA_VERSION_KEYS = ("schemaVersion", "schema_version")


class ManifestAdapterError(ValueError):
    """A manifest was too malformed to build an operator-seat structure from.

    Raised by the builder functions (``operatorapp_from_manifest`` and friends)
    when a REQUIRED facet or field is missing or the wrong type. Callers that want
    to collect every problem instead of failing on the first one should call
    :func:`validate_manifest` and inspect the returned :class:`ManifestError` list.
    """


@dataclass(frozen=True)
class ManifestError:
    """One structured validation finding against the v1.2 manifest contract.

    Attributes:
        facet: Which part of the manifest the problem is in. One of ``"root"``,
            ``"operator"``, ``"install"``, ``"knowledge"`` or ``"signature"``.
        field: The dotted path to the offending field (e.g. ``"operator.conditions"``
            or ``"install.steps[2].script"``).
        message: A human-readable description of the violation.
    """

    facet: str
    field: str
    message: str


@dataclass(frozen=True)
class InstallStep:
    """One typed, ordered step descriptor from the manifest ``install`` facet.

    A PURE description of an install action, never its execution. ``kind`` is one
    of :data:`STEP_KINDS`; ``params`` carries the kind-specific fields verbatim
    from the manifest (minus ``kind`` itself), so the provisioner (OPS1.x) has
    everything it declared without this layer inventing or dropping fields.

    Attributes:
        kind: The step kind, one of :data:`STEP_KINDS`.
        params: The remaining step fields, exactly as declared in the manifest.
    """

    kind: str
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InstallPlan:
    """An ordered, immutable list of install step descriptors.

    Built from the manifest ``install`` facet, preserving declaration order. A v1.1
    manifest (or any v1.2 manifest without an ``install`` facet) yields an EMPTY
    plan (``steps == ()``), which reads as "nothing to install", not an error.

    Attributes:
        steps: The ordered install steps.
    """

    steps: tuple[InstallStep, ...] = ()

    @property
    def is_empty(self) -> bool:
        """True when the plan has no steps (nothing to install)."""
        return not self.steps


@dataclass(frozen=True)
class KnowledgeSource:
    """The retriever descriptor from the manifest ``knowledge`` facet.

    Declares WHERE a capability's knowledge lives (namespace, search function,
    graph, kinds, KEDB linkage, retriever factory), never what Atlas may DO with
    it (the knowledge-is-not-policy carve-out of the parent spec). Consumed by the
    seat's knowledge-facet wiring (OPS2.2) and the dashboard Ops Wiki tab.

    Attributes:
        namespace: The DB / logical namespace the knowledge lives in (e.g. ``"ops"``).
        search_fn: The hybrid search function name (e.g. ``"ops.hybrid_search_ops"``).
        graph: The knowledge graph name, or None when the facet declares none.
        kinds: The content kinds served (e.g. runbook, known-error, postmortem).
        kedb: Whether this source is linked to the ITIL KEDB.
        retriever: The retriever-factory reference (``"module:callable"``), or None.
    """

    namespace: str
    search_fn: str
    graph: str | None = None
    kinds: tuple[str, ...] = ()
    kedb: bool = False
    retriever: str | None = None


# --- internal helpers --------------------------------------------------------


def _require_mapping(manifest: Any) -> dict:
    """Return the manifest as a plain mapping or raise ManifestAdapterError."""
    if not isinstance(manifest, Mapping):
        raise ManifestAdapterError(f"manifest must be a mapping, got {type(manifest).__name__}")
    return dict(manifest)


def _operator_facet(manifest: Mapping[str, Any]) -> dict:
    """Return the required ``operator`` facet or raise ManifestAdapterError."""
    operator = manifest.get("operator")
    if not isinstance(operator, Mapping):
        raise ManifestAdapterError(
            "manifest is missing a well-formed 'operator' facet "
            f"(got {type(operator).__name__})"
        )
    return dict(operator)


def _schema_version(manifest: Mapping[str, Any]) -> Any:
    """Return the declared schema version under either accepted key, or None."""
    for key in _SCHEMA_VERSION_KEYS:
        if key in manifest:
            return manifest[key]
    return None


# --- builders ----------------------------------------------------------------


def manifest_id(manifest: Mapping[str, Any]) -> str:
    """Return the manifest ``id``, or raise ManifestAdapterError if absent/blank."""
    manifest = _require_mapping(manifest)
    ident = manifest.get("id")
    if not isinstance(ident, str) or not ident:
        raise ManifestAdapterError(f"manifest requires a non-empty str 'id', got {ident!r}")
    return ident


def operatorapp_from_manifest(manifest: Mapping[str, Any]) -> dict:
    """Build an Operatorapp spec from the manifest ``operator`` facet.

    Mirrors ``registration.derive_operatorapp_spec`` output shape by running the
    same normalizer (``fleet.operatorapp.normalize_operatorapp_spec``), so a
    manifest-discovered app registers identically to the built-in seven. The
    ``conditions`` and ``proposedStandardActions`` are taken DIRECTLY from the
    operator facet (the manifest already declares the derived standard+reversible
    set), and ``ratifiedStandardActions`` is always empty here: ratification is a
    human-only field the seat never writes.

    Args:
        manifest: A parsed skworld.module.json mapping with an ``operator`` facet.

    Returns:
        A normalized Operatorapp spec dict: ``name`` (the manifest id), ``cli``,
        ``repos``, ``contractVersion``, ``proposedStandardActions``,
        ``ratifiedStandardActions`` (empty), ``conditions``, ``deleted``.

    Raises:
        ManifestAdapterError: the manifest has no id or no operator facet.
        fleet.operatorapp.OperatorappSpecError: a facet field is malformed
            (non-str cli, non-int contractVersion, malformed list, ...).
    """
    manifest = _require_mapping(manifest)
    name = manifest_id(manifest)
    operator = _operator_facet(manifest)
    return operatorapp.normalize_operatorapp_spec(
        {
            "name": name,
            "cli": operator.get("cli"),
            "repos": list(operator.get("repos", [])),
            "contractVersion": operator.get("contractVersion", 1),
            "proposedStandardActions": list(operator.get("proposedStandardActions", [])),
            "conditions": list(operator.get("conditions", [])),
        }
    )


def install_plan_from_manifest(manifest: Mapping[str, Any]) -> InstallPlan:
    """Build an ordered InstallPlan from the manifest ``install`` facet.

    Each step becomes a typed :class:`InstallStep` (``kind`` + the remaining
    fields verbatim), preserving declaration order. A manifest with no ``install``
    facet (every v1.1 manifest, and v1.2 manifests that only observe) yields an
    EMPTY plan; that is a valid "nothing to install" result, not an error.

    Args:
        manifest: A parsed skworld.module.json mapping.

    Returns:
        The ordered install plan (possibly empty).

    Raises:
        ManifestAdapterError: the ``install`` facet is present but malformed, or a
            step is not a mapping, has no/unknown ``kind``, or is missing a field
            required for its kind.
    """
    manifest = _require_mapping(manifest)
    install = manifest.get("install")
    if install is None:
        return InstallPlan(steps=())
    if not isinstance(install, Mapping):
        raise ManifestAdapterError(
            f"'install' facet must be a mapping, got {type(install).__name__}"
        )
    raw_steps = install.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ManifestAdapterError(
            f"'install.steps' must be a list, got {type(raw_steps).__name__}"
        )
    steps: list[InstallStep] = []
    for index, raw in enumerate(raw_steps):
        steps.append(_install_step(index, raw))
    return InstallPlan(steps=tuple(steps))


def _install_step(index: int, raw: Any) -> InstallStep:
    """Build one InstallStep from a raw manifest step, validating kind + fields."""
    if not isinstance(raw, Mapping):
        raise ManifestAdapterError(
            f"install.steps[{index}] must be a mapping, got {type(raw).__name__}"
        )
    kind = raw.get("kind")
    if kind not in STEP_KINDS:
        raise ManifestAdapterError(
            f"install.steps[{index}] has unknown kind {kind!r} "
            f"(expected one of {sorted(STEP_KINDS)})"
        )
    for required in _STEP_REQUIRED_FIELDS[kind]:
        if required not in raw:
            raise ManifestAdapterError(
                f"install.steps[{index}] (kind {kind!r}) missing required field " f"{required!r}"
            )
    params = {k: v for k, v in raw.items() if k != "kind"}
    return InstallStep(kind=kind, params=params)


def knowledge_source_from_manifest(
    manifest: Mapping[str, Any],
) -> KnowledgeSource | None:
    """Build a KnowledgeSource from the manifest ``knowledge`` facet, or None.

    A manifest with no ``knowledge`` facet returns None (the app declares no
    retriever, which is valid). When the facet IS present it must at least name a
    ``namespace`` and a ``search_fn``; ``graph``, ``kinds``, ``kedb`` and
    ``retriever`` are optional.

    Args:
        manifest: A parsed skworld.module.json mapping.

    Returns:
        The knowledge-source descriptor, or None when no facet is declared.

    Raises:
        ManifestAdapterError: the ``knowledge`` facet is present but malformed, or
            missing ``namespace`` / ``search_fn``.
    """
    manifest = _require_mapping(manifest)
    knowledge = manifest.get("knowledge")
    if knowledge is None:
        return None
    if not isinstance(knowledge, Mapping):
        raise ManifestAdapterError(
            f"'knowledge' facet must be a mapping, got {type(knowledge).__name__}"
        )
    namespace = knowledge.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise ManifestAdapterError(
            f"knowledge.namespace must be a non-empty str, got {namespace!r}"
        )
    search_fn = knowledge.get("search_fn")
    if not isinstance(search_fn, str) or not search_fn:
        raise ManifestAdapterError(
            f"knowledge.search_fn must be a non-empty str, got {search_fn!r}"
        )
    raw_kinds = knowledge.get("kinds", [])
    if not isinstance(raw_kinds, list) or not all(isinstance(k, str) for k in raw_kinds):
        raise ManifestAdapterError(f"knowledge.kinds must be a list of str, got {raw_kinds!r}")
    graph = knowledge.get("graph")
    if graph is not None and not isinstance(graph, str):
        raise ManifestAdapterError(f"knowledge.graph must be a str when present, got {graph!r}")
    retriever = knowledge.get("retriever")
    if retriever is not None and not isinstance(retriever, str):
        raise ManifestAdapterError(
            f"knowledge.retriever must be a str when present, got {retriever!r}"
        )
    return KnowledgeSource(
        namespace=namespace,
        search_fn=search_fn,
        graph=graph,
        kinds=tuple(raw_kinds),
        kedb=bool(knowledge.get("kedb", False)),
        retriever=retriever,
    )


# --- validation --------------------------------------------------------------


def _has_signature(manifest: Mapping[str, Any]) -> bool:
    """True when the manifest carries a non-empty signature envelope.

    This models the signed-manifest CONTRACT only. It does NOT verify the
    signature (no gpg, no capauth): OPS0.3 / OPS1.x own real verification. A
    manifest is considered "signed" here when it carries a truthy ``signature``
    (or ``signed``) field, which is what the wiring card will later hand a real
    verifier.
    """
    return bool(manifest.get("signature") or manifest.get("signed"))


def _validate_operator(operator: Any) -> list[ManifestError]:
    """Structured validation of the required ``operator`` facet."""
    errors: list[ManifestError] = []
    if not isinstance(operator, Mapping):
        return [
            ManifestError(
                "operator",
                "operator",
                f"operator facet is required and must be a mapping, "
                f"got {type(operator).__name__}",
            )
        ]
    cli = operator.get("cli")
    if cli is not None and (not isinstance(cli, str) or not cli):
        errors.append(ManifestError("operator", "operator.cli", "cli must be a non-empty str"))
    contract_version = operator.get("contractVersion", 1)
    if not isinstance(contract_version, int) or isinstance(contract_version, bool):
        errors.append(
            ManifestError(
                "operator",
                "operator.contractVersion",
                "contractVersion must be an int",
            )
        )
    for list_field in ("conditions", "proposedStandardActions", "repos"):
        value = operator.get(list_field, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            errors.append(
                ManifestError(
                    "operator",
                    f"operator.{list_field}",
                    f"{list_field} must be a list of non-empty str",
                )
            )
    return errors


def _validate_install(install: Any) -> list[ManifestError]:
    """Structured validation of the optional ``install`` facet."""
    if install is None:
        return []
    if not isinstance(install, Mapping):
        return [
            ManifestError(
                "install",
                "install",
                f"install facet must be a mapping, got {type(install).__name__}",
            )
        ]
    raw_steps = install.get("steps", [])
    if not isinstance(raw_steps, list):
        return [
            ManifestError(
                "install",
                "install.steps",
                f"install.steps must be a list, got {type(raw_steps).__name__}",
            )
        ]
    errors: list[ManifestError] = []
    for index, step in enumerate(raw_steps):
        prefix = f"install.steps[{index}]"
        if not isinstance(step, Mapping):
            errors.append(ManifestError("install", prefix, "step must be a mapping"))
            continue
        kind = step.get("kind")
        if kind not in STEP_KINDS:
            errors.append(
                ManifestError(
                    "install",
                    f"{prefix}.kind",
                    f"unknown step kind {kind!r} (expected one of " f"{sorted(STEP_KINDS)})",
                )
            )
            continue
        for required in _STEP_REQUIRED_FIELDS[kind]:
            if required not in step:
                errors.append(
                    ManifestError(
                        "install",
                        f"{prefix}.{required}",
                        f"kind {kind!r} requires field {required!r}",
                    )
                )
    return errors


def _validate_knowledge(knowledge: Any) -> list[ManifestError]:
    """Structured validation of the optional ``knowledge`` facet."""
    if knowledge is None:
        return []
    if not isinstance(knowledge, Mapping):
        return [
            ManifestError(
                "knowledge",
                "knowledge",
                f"knowledge facet must be a mapping, got {type(knowledge).__name__}",
            )
        ]
    errors: list[ManifestError] = []
    for req in ("namespace", "search_fn"):
        value = knowledge.get(req)
        if not isinstance(value, str) or not value:
            errors.append(
                ManifestError(
                    "knowledge",
                    f"knowledge.{req}",
                    f"{req} must be a non-empty str",
                )
            )
    kinds = knowledge.get("kinds", [])
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        errors.append(ManifestError("knowledge", "knowledge.kinds", "kinds must be a list of str"))
    return errors


def validate_manifest(manifest: Any, *, require_signed: bool = False) -> list[ManifestError]:
    """Validate a manifest against the v1.2 contract, returning structured errors.

    PURE and total: it never raises on a malformed manifest, it COLLECTS every
    problem into a list of :class:`ManifestError` so a caller (discovery, the skos
    planner, a doctor check) can report them all at once. An empty list means the
    manifest satisfies the modelled contract.

    Signature handling models the contract only: with ``require_signed=True`` a
    manifest that carries no signature envelope yields a ``signature`` error. Real
    cryptographic verification is deliberately NOT done here (OPS0.3 / OPS1.x).

    Args:
        manifest: The parsed manifest (any type; a non-mapping is itself an error).
        require_signed: When True, a manifest without a signature envelope is an
            error (the discovery trust bar: unsigned manifests are not loaded).

    Returns:
        A list of structured errors; empty when the manifest is contract-valid.
    """
    if not isinstance(manifest, Mapping):
        return [
            ManifestError(
                "root",
                "manifest",
                f"manifest must be a mapping, got {type(manifest).__name__}",
            )
        ]

    errors: list[ManifestError] = []

    ident = manifest.get("id")
    if not isinstance(ident, str) or not ident:
        errors.append(ManifestError("root", "id", "manifest requires a non-empty str 'id'"))

    if _schema_version(manifest) is None:
        errors.append(
            ManifestError(
                "root",
                "schemaVersion",
                "manifest requires a schema version (schemaVersion / schema_version)",
            )
        )

    errors.extend(_validate_operator(manifest.get("operator")))
    errors.extend(_validate_install(manifest.get("install")))
    errors.extend(_validate_knowledge(manifest.get("knowledge")))

    if require_signed and not _has_signature(manifest):
        errors.append(
            ManifestError(
                "signature",
                "signature",
                "manifest must carry a signature envelope to be discovered "
                "(unsigned manifests are not loaded)",
            )
        )

    return errors


__all__ = [
    "STEP_KINDS",
    "ManifestAdapterError",
    "ManifestError",
    "InstallStep",
    "InstallPlan",
    "KnowledgeSource",
    "manifest_id",
    "operatorapp_from_manifest",
    "install_plan_from_manifest",
    "knowledge_source_from_manifest",
    "validate_manifest",
]
