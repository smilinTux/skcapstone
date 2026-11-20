"""Fleet operator adapter: the reference implementation of the adapter contract.

The one operator observes and acts on every app through the same three verbs
(explain, observe, act). This module is the fleet's conformant implementation of
the read side (explain + observe), gathered from the Phase 4-7 controllers. The
act verb is wired in the fleet adapter CLI card; observe here writes nothing.
"""

from __future__ import annotations

import logging
import re

from ..fleet import (
    agent_controller,
    config_controller,
    cron_controller,
    modelserver_controller,
    node_controller,
    service_controller,
    store,
)
from ..fleet.paths import self_node_name
from ..fleet.services import ServiceSpecError, normalize_service_spec
from . import actuator

logger = logging.getLogger(__name__)

#: Reversible ops the operator may apply through the fleet act verb, mapped to
#: the object kind they annotate. Irreversible or major actions never reach the
#: act verb (they escalate and park), so they are deliberately absent here.
_ACTION_KIND = {
    "rerun_cronjob": "cronjob",
    "restart_service": "service",
    "replace_workload": "service",
}

#: Condition types that indicate a PROBLEM when their status is "True" (the rest
#: are health conditions that indicate a problem when "False"). The operator
#: brief uses this polarity to decide what is firing.
PROBLEM_WHEN_TRUE = frozenset(
    {
        "MissedRun",
        "ConfigDrift",
        "RotationOverdue",
        "CrashLooping",
        "SyncConflict",
        "MemoryPressure",
        "DiskPressure",
    }
)

#: The fleet action catalog the operator may draw on (adapter-contract shape).
_ACTIONS = [
    {
        "name": "rerun_cronjob",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "re-run a missed CronJob",
        "kedb_refs": [],
    },
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart a failed Service unit",
        "kedb_refs": [],
    },
    {
        "name": "replace_workload",
        "standard": False,
        "reversible": True,
        "blast_radius": "medium",
        "runbook": "re-place a workload on another node",
        "kedb_refs": [],
    },
    {
        "name": "drain_node",
        "standard": False,
        "reversible": True,
        "blast_radius": "drain_always_on",
        "runbook": "drain a node (parks if always-on)",
        "kedb_refs": [],
    },
    {
        "name": "delete_object",
        "standard": False,
        "reversible": False,
        "blast_radius": "delete",
        "runbook": "tombstone a fleet object",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


def fleet_explain() -> dict:
    """The fleet's self-description in the adapter-contract shape."""
    from ..fleet.explain import KINDS

    conditions = sorted(
        {
            "MissedRun",
            "AgentReady",
            "Serving",
            "SecretPresent",
            "ConfigDrift",
            "RotationOverdue",
            "Ready",
        }
        | set(PROBLEM_WHEN_TRUE)
    )
    return {
        "kinds": sorted(KINDS),
        "conditions": conditions,
        "actions": [dict(a) for a in _ACTIONS],
    }


def _status_written_at(paths, kind: str, name: str, node: str | None) -> str | None:
    """When the node daemon last WROTE the status a row was derived from, or None.

    The controllers below pick the status of the placed node when there is one,
    else the first status on disk (``store.merged`` order). This picks the same
    file. Status files are write-on-change (``store.write_status``), so
    ``updatedAt`` is the last time sknoded observed this content and found it
    different: the data was certainly true then. It is a lower bound, never an
    invented "now": an unchanged reading ages, and old evidence is refused at the
    decision point rather than passed off as fresh.
    """
    if node is not None:
        status = store.read_status(paths, kind, name, node)
    else:
        statuses = (store.merged(paths, kind, name) or {}).get("statuses") or []
        status = statuses[0] if statuses else None
    stamp = (status or {}).get("updatedAt")
    return stamp if isinstance(stamp, str) and stamp else None


def _cond(ctype: str, status, obj: str, source_time: str | None) -> dict:
    item = {"type": ctype, "status": status, "object": obj}
    if source_time is not None:
        item["source_observed_at"] = source_time
    return item


def fleet_observe(paths, now_iso: str) -> dict:
    """Read-only snapshot of every fleet object's key condition(s).

    Never writes: the operator's observe verb only reads derived state.

    Source time (card 21b01d01): each condition carries ``source_observed_at``,
    the ``updatedAt`` its node wrote on the file the condition was derived from
    (the object's status file, or ``node.json`` for node conditions). The pass
    clock ``now_iso`` is NOT a source time and is never reported as one: it only
    says when this pass evaluated the files. A condition with no status file has
    no source time and therefore cannot be actuated on (operator_seat.evidence).
    """
    conds: list[dict] = []

    for nv in node_controller.node_views(paths):
        report = store.read_node_file(paths, nv.name, "node.json") or {}
        stamp = report.get("updatedAt") if isinstance(report.get("updatedAt"), str) else None
        for c in nv.conditions:
            conds.append(_cond(c.get("type"), c.get("status"), nv.name, stamp))

    for row in cron_controller.cron_rows(paths, now_iso):
        stamp = _status_written_at(paths, "cronjob", row.name, row.node)
        conds.append(_cond("MissedRun", _b(row.missed), row.name, stamp))

    for row in agent_controller.agent_rows(paths, now_iso):
        stamp = _status_written_at(paths, "agent", row.name, row.node)
        conds.append(_cond("AgentReady", row.ready, row.name, stamp))

    for row in modelserver_controller.modelserver_rows(paths, now_iso):
        stamp = _status_written_at(paths, "modelserver", row.name, row.node)
        conds.append(_cond("Serving", _b(row.serving), row.name, stamp))

    for row in config_controller.config_rows(paths, now_iso):
        stamp = _status_written_at(paths, "config", row.name, row.node)
        conds.append(_cond("SecretPresent", _b(row.secrets_present), row.name, stamp))
        conds.append(_cond("ConfigDrift", _b(row.drift), row.name, stamp))
        conds.append(_cond("RotationOverdue", _b(row.rotation_overdue), row.name, stamp))

    for row in service_controller.service_rows(paths):
        stamp = _status_written_at(paths, "service", row.name, row.node)
        conds.append(_cond("Ready", row.ready, row.name, stamp))

    return {"conditions": conds}


#: Sentinel: "use the local CapAuth trust roster" (``signing.capauth_verifier``).
#: Passing ``None`` explicitly means "no verifier", which refuses.
DEFAULT_VERIFIER = object()


def fleet_target_known(paths, proposal: dict, *, verifier=DEFAULT_VERIFIER) -> bool:
    """True when the proposal's object resolves to an existing fleet object.

    The decision layer validates the ACTION against the catalog but had no way
    to check the TARGET, so a proposal naming something that does not exist
    still classified auto and was handed to the act verb, which then raised.
    Actions with no ops-channel mapping return True: the action-level check
    already governs those, and this must not second-guess that disposition.

    ``restart_service`` is judged by ``resolve_restart_target`` itself (card
    21b01d01): a target this seat would refuse to restart is not a known target,
    so the planner escalates it and the dispatcher refuses it in preflight,
    before the approved change is spent or EXECUTING is written.
    """
    action = proposal.get("action")
    if action == "restart_service":
        try:
            resolve_restart_target(paths, proposal, verifier=verifier)
        except actuator.NotAttemptedError:
            return False
        return True
    kind = _ACTION_KIND.get(action)
    if kind is None:
        return True
    return store.read_spec(paths, kind, proposal.get("object")) is not None


#: How long the postcondition may wait for this node's sknoded to re-observe a
#: restarted unit. Converge runs every ``converge.ACTUATION_INTERVAL_S`` (30 s)
#: and writes status only on change, so two passes plus slack covers it.
RESTART_SETTLE_S = 75
RESTART_POLL_S = 5

#: A plain systemd unit name: no whitespace, no leading dash (never an option to
#: systemctl), no path separators.
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@:-]+$")

#: systemd unit types other than ``service``. A spec.unit ending in one of these
#: is refused: restarting a target, timer or socket is not "restart a service".
_NON_SERVICE_TYPES = frozenset(
    {"target", "timer", "socket", "mount", "automount", "swap", "path", "slice", "scope", "device"}
)


def _service_unit(name: str, raw: str) -> str:
    """``raw`` as a ``.service`` unit name, or refuse. A bare name gets ``.service``."""
    if not _UNIT_RE.match(raw) or raw.startswith("-"):
        raise actuator.NotAttemptedError(
            f"service {name!r} spec.unit {raw!r} is not a plain systemd unit name"
        )
    if raw.endswith(".service"):
        return raw
    suffix = raw.rsplit(".", 1)[-1] if "." in raw else ""
    if suffix in _NON_SERVICE_TYPES:
        raise actuator.NotAttemptedError(
            f"service {name!r} spec.unit {raw!r} is a .{suffix} unit; only .service units "
            "are restarted"
        )
    return f"{raw}.service"


def _restartable(name: str, payload: dict) -> tuple[dict, str]:
    """The normalized spec and ``.service`` unit of a restartable object, or refuse."""
    raw = payload.get("spec") or {}
    if not raw.get("unit"):
        raise actuator.NotAttemptedError(
            f"service {name!r} has no unit mapping (spec.unit); refusing to guess"
        )
    try:
        spec = normalize_service_spec(raw)
    except ServiceSpecError as exc:
        raise actuator.NotAttemptedError(f"service {name!r} spec invalid: {exc}") from exc
    unit = _service_unit(name, spec["unit"])
    if spec["deleted"]:
        raise actuator.NotAttemptedError(f"service {name!r} is deleted; not restarted")
    if spec["paused"]:
        raise actuator.NotAttemptedError(f"service {name!r} is paused; not restarted")
    if spec["runtime"] != "systemd-user":
        raise actuator.NotAttemptedError(
            f"service {name!r} runtime {spec['runtime']!r} has no restart muscle here"
        )
    return spec, unit


def _placed_here(paths, name: str, local_node: str | None) -> tuple[dict, str]:
    placement = store.read_placement(paths, "service", name)
    node = (placement or {}).get("node")
    if not node:
        raise actuator.NotAttemptedError(
            f"service {name!r} has no placement; cannot tell which node runs it"
        )
    here = local_node or self_node_name()
    if node != here:
        raise actuator.NotAttemptedError(
            f"service {name!r} is placed on {node!r}, this seat runs on {here!r}; "
            "no reviewed remote actuation path exists, refusing"
        )
    return placement, node


def resolve_restart_target(
    paths, proposal: dict, *, local_node: str | None = None, verifier=DEFAULT_VERIFIER
) -> dict:
    """Resolve a fleet ``restart_service`` proposal to a concrete unit on THIS node.

    Returns ``{"object", "unit", "node", "runtime"}`` or raises
    ``actuator.NotAttemptedError`` with the reason. Fails closed, never guesses:

    * only ``restart_service`` has physical muscle; ``rerun_cronjob`` and
      ``replace_workload`` are refused rather than run as a unit restart;
    * the unit is ``spec.unit`` of the fleet Service object, the same field
      sknoded's converge actuates (``services.normalize_service_spec``). It must
      be a ``.service`` unit (a bare name is normalized to one). The object name
      is never used as a unit, and a proposal that names a different unit is
      refused (the brain does not choose the unit);
    * the object must be placed, and placed on this node. There is no reviewed
      remote actuation path (the operator plane's ``act`` route is reserved and
      never actuates), so an object placed elsewhere is refused, not restarted
      locally;
    * only the ``systemd-user`` runtime is restarted; a paused or deleted object
      is refused;
    * the spec and placement must verify against the trust roster, with the same
      ``converge.verify_desired`` sknoded uses before healing. Unlike converge,
      this is not gated by the signing rollout mode: an autonomous seat never
      restarts on an unsigned spec. Limit: the roster always trusts this seat's
      own key, and permissive signing signs every local write with it, so this
      proves "written on a trusted seat", not "authored by Chef". The real
      guards are placement on this node, the .service suffix and the rollback
      binding;
    * the unit must be allowed for this node's install profile, with the same
      ``profiles.unit_allowed`` converge uses (not gated by the profile rollout
      mode either).
    """
    from ..fleet import converge, profiles, signing

    action = proposal.get("action")
    if action != "restart_service":
        raise actuator.NotAttemptedError(
            f"fleet action {action!r} has no physical muscle on the honor path"
        )
    name = proposal.get("object")
    payload = store.read_spec(paths, "service", name) if isinstance(name, str) else None
    if payload is None:
        raise actuator.NotAttemptedError(f"unknown service object {name!r}")
    spec, unit = _restartable(name, payload)
    claimed = proposal.get("unit")
    if claimed is not None and claimed not in (unit, spec["unit"]):
        raise actuator.NotAttemptedError(
            f"proposal names unit {claimed!r} but service {name!r} maps to {unit!r}"
        )
    placement, node = _placed_here(paths, name, local_node)
    trust = signing.capauth_verifier() if verifier is DEFAULT_VERIFIER else verifier
    verified, detail = converge.verify_desired(payload, placement, trust)
    if not verified:
        raise actuator.NotAttemptedError(f"service {name!r} not verified: {detail}")
    role = profiles.profile_of(store.read_spec(paths, "node", node)) or ""
    manifests = profiles.manifest_dir(paths)
    for candidate in {spec["unit"], unit}:
        if not profiles.unit_allowed(role, candidate, manifests=manifests):
            raise actuator.NotAttemptedError(
                f"unit {candidate!r} is forbidden for node {node!r} profile {role!r}"
            )
    return {"object": name, "unit": unit, "node": node, "runtime": spec["runtime"]}


def recheck_restart_target(paths, target: dict, *, local_node: str | None = None) -> None:
    """Re-check pause, deletion, unit and placement immediately before the restart.

    The full resolution (including the signature) ran before the ITIL record and the
    ``operatorActions`` annotation; this closes the window between them and systemctl.
    The annotation itself re-writes the spec, so the signature is not re-judged here.
    """
    name = target["object"]
    payload = store.read_spec(paths, "service", name)
    if payload is None:
        raise actuator.NotAttemptedError(f"service {name!r} disappeared before the restart")
    _spec, unit = _restartable(name, payload)
    if unit != target["unit"]:
        raise actuator.NotAttemptedError(
            f"service {name!r} unit changed to {unit!r} before the restart"
        )
    _placed_here(paths, name, local_node)


def _acting_identity() -> str:
    """The capauth identity of the seat performing this action.

    Split out so the failure policy lives at the call site: attribution must
    never be the reason the fleet cannot act.
    """
    from capauth import resolve_agent_identity

    ident = resolve_agent_identity()
    return getattr(ident, "capauth_uri", "") or getattr(ident, "agent", "") or ""


def _operator_action_entry(
    *, action: str, now_iso: str, classification: dict, proposal: dict
) -> dict:
    """One operatorActions record, attributed to the RESOLVED acting identity.

    `by` was the literal string "atlas". An audit line naming a constant
    attributes nothing: every seat, on every node, forever, claims to be the
    same actor, so the field cannot answer the only question an audit entry
    exists to answer. It now carries the capauth identity.

    A resolver failure degrades to "unattributed" rather than raising: an entry
    that says it does not know who acted is honest and still records that
    something acted. Refusing to act because attribution failed would trade a
    provenance gap for an availability outage, which is the wrong trade for an
    ops channel (permissive posture, spec section 7).
    """
    try:
        by = _acting_identity() or "unattributed"
    except Exception as exc:  # noqa: BLE001
        logger.warning("fleet_adapter: identity resolve failed, entry unattributed: %s", exc)
        by = "unattributed"
    return {
        "action": action,
        "ts": now_iso,
        "by": by,
        "changeClass": classification.get("change_class"),
        "rationale": proposal.get("rationale", ""),
    }


def fleet_act(paths, proposal: dict, classification: dict, *, now_iso: str, writer=None) -> dict:
    """Apply an operator proposal to the fleet: the act verb (ops channel).

    Records the action as an attributed entry on the target object's spec
    (`operatorActions`), so every operator touch is auditable and reversible.
    The ENTRY itself is not signed; the spec WRITE carries the signature, in
    the writer block (`writer.signature`, suite named by `writer.suite_id`),
    which covers the whole payload including this entry. Saying the entry is
    signed would invite trust it has not earned.
    Refuses when the fleet is frozen (belt-and-suspenders: the loop already
    checks freeze first). Only reversible ops are mapped; anything else raises,
    since majors and irreversible actions escalate and never reach the act verb.
    The writer is the autonomous seat (agent_seat=True): it may write object
    specs but never plane files.
    """
    if store.is_frozen(paths):
        raise RuntimeError("fleet is frozen: the operator does not actuate")
    action = proposal.get("action")
    kind = _ACTION_KIND.get(action)
    if kind is None:
        raise ValueError(f"no ops-channel mapping for action {action!r}")
    name = proposal.get("object")
    existing = store.read_spec(paths, kind, name)
    if existing is None:
        raise ValueError(f"unknown {kind} object {name!r}")
    spec = dict(existing.get("spec", {}))
    log = list(spec.get("operatorActions", []))
    entry = _operator_action_entry(
        action=action, now_iso=now_iso, classification=classification, proposal=proposal
    )
    # A standing condition (an app that is down reads stale, so the brief is
    # never quiet) re-proposes the same fix every pass. Escalations already
    # dedupe on a content-based decision id so a standing issue is ONE decision
    # a human resolves once; the act path had no equivalent and appended a fresh
    # entry every 15 minutes, growing operatorActions without bound. Collapse a
    # repeat of the immediately preceding action into count + lastTs, so the
    # signal survives ("still happening, N times") without the unbounded log.
    prev = log[-1] if log else None
    if prev and prev.get("action") == action and prev.get("changeClass") == entry["changeClass"]:
        collapsed = dict(prev)
        collapsed["count"] = int(collapsed.get("count", 1)) + 1
        collapsed["lastTs"] = now_iso
        collapsed["rationale"] = entry["rationale"]
        log[-1] = collapsed
    else:
        entry["count"] = 1
        log.append(entry)
    spec["operatorActions"] = log
    writer = writer or store.Writer(
        role="operator",
        node=self_node_name(),
        identity=store.resolved_writer_identity(),
        agent_seat=True,
    )
    return store.write_spec(paths, kind, name, spec, writer=writer)


__all__ = [
    "fleet_explain",
    "fleet_observe",
    "fleet_act",
    "resolve_restart_target",
    "recheck_restart_target",
    "PROBLEM_WHEN_TRUE",
]
