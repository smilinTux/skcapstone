"""Profile kind model: role to install profile (epic 3bbf39ea, card de9cf1d0).

The fleet already knows how to schedule work onto nodes. What it never knew
is what a node of a given role is *supposed to have installed*: which
packages, which user-scope units may be enabled, which Syncthing folders
it joins, and which capauth identity class it holds. That is this module.

Two axes stay ORTHOGONAL and neither is derived from the other:

  service role  what runs here (control, builder-standby, worker-gpu, observer)
  state tier    how much sovereign state lives here (full-replica,
                control-bus, none)

Conflating them is exactly how a GPU worker ended up carrying agent memories,
sessions and stale source checkouts it never needed. A role is expressed by
the profile object's *name*; the tier is an explicit field on its spec.

The spec side only: this module reads nothing FROM THE HOST. It runs no
commands and asks systemd nothing; the only files it opens are the profile
manifests, which are declared state, not observation. Observation lives in
nodeinventory.py and the drift diff in profile_doctor.py, which is what
keeps a validator that can veto a node free of the machine it judges.

A spec that fails validation must never reach an actuation verb; callers
treat ProfileSpecError as "do not touch this node" (degrade-safe, the same
contract as services.ServiceSpecError).

Card 2551d698 adds the two read helpers converge.py consumes, profile_of()
and unit_allowed(). Their contract is the opposite of the validator's: they
answer a question ABOUT a running node, so every failure to answer resolves
to "allowed". A missing, unreadable or invalid manifest, an unknown role and
an unbound node all mean True. A gate that failed closed on a file that has
not synced yet would refuse to heal services mid-install, which is a worse
outage than the drift it is trying to catch.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .paths import FleetPaths, valid_name

#: How much sovereign state a node of this profile carries. Independent of
#: the service role: a builder-standby holds a full replica while running
#: almost nothing, and a worker runs a lot while holding nothing.
STATE_TIERS = frozenset({"full-replica", "control-bus", "none"})

#: The capauth identity class the node's credential belongs to. `operator`
#: is the human/AI ops seat, `agent` a sovereign agent identity, `worker` a
#: least-privilege node credential, `observer` read-only.
IDENTITY_CLASSES = frozenset({"operator", "agent", "worker", "observer"})

#: The three name lists every package/unit set carries.
_NAME_LIST_FIELDS = ("required", "allowed", "mustNot")

#: Rollout flag for the converge-side profile gate (card 57357411), read the
#: same way signing.SIGNING_ENV is: off by default, so shipping this code
#: changes nothing until an operator opts a run in.
PROFILE_GATE_ENV = "SKFLEET_PROFILE_GATE"

#: off never asks the question, shadow reports it, enforce additionally
#: refuses to HEAL a unit its role forbids. No mode ever stops a unit.
GATE_MODES = frozenset({"off", "shadow", "enforce"})

#: Operator override for where the manifests live. When set it is
#: AUTHORITATIVE: an unreadable override means "no manifests", hence allow
#: everything. Falling back to a different set behind the operator's back
#: would make the gate answer from a manifest nobody pointed it at.
MANIFEST_DIR_ENV = "SKFLEET_PROFILE_MANIFESTS"

#: Where the manifests ship in a source checkout, relative to the repo root
#: (decision card c5ad2471). An installed wheel carries no such directory,
#: which is why the fleet tree is searched first.
_SHIPPED_MANIFEST_SUBPATH = ("deploy", "fleet-objects", "profile")


class ProfileSpecError(ValueError):
    """A Profile spec is malformed and must not be converged against."""


def _name_lists(field: str, raw: object) -> dict:
    """Validate one {required, allowed, mustNot} block of names.

    Args:
        field: The owning spec field name, used in error messages.
        raw: The candidate block; None and {} both mean "three empty lists".

    Returns:
        A dict with required/allowed/mustNot as sorted, de-duplicated lists.

    Raises:
        ProfileSpecError: The block is not a dict, carries an unknown key,
            holds anything but non-empty strings, lists the same name in
            both allowed and mustNot, or requires a name it does not allow.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ProfileSpecError(f"spec.{field} must be a dict of name lists, got {raw!r}")
    unknown = sorted(set(raw) - set(_NAME_LIST_FIELDS))
    if unknown:
        raise ProfileSpecError(
            f"spec.{field} has unknown keys {unknown} (known: {list(_NAME_LIST_FIELDS)})"
        )
    out: dict[str, list[str]] = {}
    for key in _NAME_LIST_FIELDS:
        values = raw.get(key, [])
        if not isinstance(values, list):
            raise ProfileSpecError(f"spec.{field}.{key} must be a list, got {values!r}")
        for name in values:
            if not isinstance(name, str) or not name.strip():
                raise ProfileSpecError(
                    f"spec.{field}.{key} entries must be non-empty names, got {name!r}"
                )
        out[key] = sorted({name.strip() for name in values})

    # A name that is both allowed and forbidden makes the drift report
    # non-deterministic: converge could justify either verdict.
    contradictory = sorted(set(out["allowed"]) & set(out["mustNot"]))
    if contradictory:
        raise ProfileSpecError(
            f"spec.{field}: {contradictory} appear in both 'allowed' and 'mustNot'; "
            "a name cannot be permitted and forbidden at once"
        )

    # Requiring what you do not allow is the same contradiction one step out.
    # Not auto-widened: the manifest must say what it means.
    unallowed = sorted(set(out["required"]) - set(out["allowed"]))
    if unallowed:
        raise ProfileSpecError(
            f"spec.{field}: {unallowed} are in 'required' but not in 'allowed'; "
            "list every required name in 'allowed' too"
        )
    return out


def _str_list(field: str, raw: object) -> list[str]:
    """Validate a flat list of non-empty strings, sorted and de-duplicated."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ProfileSpecError(f"spec.{field} must be a list, got {raw!r}")
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise ProfileSpecError(
                f"spec.{field} entries must be non-empty strings, got {value!r}"
            )
    return sorted({value.strip() for value in raw})


def normalize_profile_spec(spec: dict) -> dict:
    """Return a full Profile spec with defaults applied, or raise.

    Defaults are deliberately conservative: empty name lists mean "this
    profile asserts nothing", which the drift report renders as no findings
    rather than as a fleet-wide uninstall. The two fields that carry real
    consequence, stateTier and capauthIdentityClass, have NO default: a
    profile that does not say how much state it holds or what credential it
    carries is a profile nobody should converge against.

    Args:
        spec: Raw Profile spec dict.

    Returns:
        Normalized dict with description, packages, units, unitsIgnore,
        stateTier, capauthIdentityClass, syncFolders and deleted.

    Raises:
        ProfileSpecError: spec is not a dict, stateTier or
            capauthIdentityClass is missing or unknown, description is not a
            string, or any name list fails validation.
    """
    if not isinstance(spec, dict):
        raise ProfileSpecError(f"profile spec must be a dict, got {spec!r}")

    state_tier = spec.get("stateTier")
    if state_tier is None:
        raise ProfileSpecError(
            "spec.stateTier is required (one of "
            f"{sorted(STATE_TIERS)}); it is orthogonal to the service role "
            "and must be stated, never inferred"
        )
    if state_tier not in STATE_TIERS:
        raise ProfileSpecError(f"unknown stateTier {state_tier!r} (known: {sorted(STATE_TIERS)})")

    identity_class = spec.get("capauthIdentityClass")
    if identity_class is None:
        raise ProfileSpecError(
            "spec.capauthIdentityClass is required (one of " f"{sorted(IDENTITY_CLASSES)})"
        )
    if identity_class not in IDENTITY_CLASSES:
        raise ProfileSpecError(
            f"unknown capauthIdentityClass {identity_class!r} "
            f"(known: {sorted(IDENTITY_CLASSES)})"
        )

    description = spec.get("description", "")
    if not isinstance(description, str):
        raise ProfileSpecError(f"spec.description must be a string, got {description!r}")

    return {
        "description": description,
        "packages": _name_lists("packages", spec.get("packages")),
        "units": _name_lists("units", spec.get("units")),
        # fnmatch patterns for units this profile takes no position on, so a
        # desktop box full of gpg-agent sockets does not read as drift.
        "unitsIgnore": _str_list("unitsIgnore", spec.get("unitsIgnore")),
        "stateTier": state_tier,
        "capauthIdentityClass": identity_class,
        "syncFolders": _str_list("syncFolders", spec.get("syncFolders")),
        "deleted": bool(spec.get("deleted", False)),
    }


def gate_mode() -> str:
    """The profile-gate rollout mode: off (default) | shadow | enforce.

    Mirrors signing.signing_mode() exactly, including the "an unknown value
    is off" rule, so a typo in the environment cannot arm a gate.
    """
    mode = os.environ.get(PROFILE_GATE_ENV, "off").strip().lower()
    return mode if mode in GATE_MODES else "off"


def _is_dir(path: Path) -> bool:
    """True when path is a readable directory. Never raises."""
    try:
        return path.is_dir()
    except OSError:
        return False


def _shipped_manifest_dir() -> Path | None:
    """The repo's `deploy/fleet-objects/profile`, or None outside a checkout."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*_SHIPPED_MANIFEST_SUBPATH)
        if _is_dir(candidate):
            return candidate
    return None


def manifest_dir(paths: FleetPaths | None = None) -> Path | None:
    """Where to read profile manifests from, or None when there are none.

    Search order: the operator override, then the fleet tree (where
    `skfleet apply -f deploy/fleet-objects/profile/*.json` lands them and
    where Syncthing keeps them fresh), then the shipped copy in a source
    checkout. The tree wins over the checkout because the tree is what the
    fleet actually agreed on, and a dev checkout may be arbitrarily ahead.

    Args:
        paths: The fleet tree to search, or None to skip that candidate.

    Returns:
        The first readable candidate directory, or None. None is not an
        error: it means the gate has nothing to judge against and must
        therefore permit everything.
    """
    override = os.environ.get(MANIFEST_DIR_ENV, "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if _is_dir(candidate) else None
    if paths is not None and _is_dir(paths.objects / "profile"):
        return paths.objects / "profile"
    return _shipped_manifest_dir()


def profile_of(node_payload: object) -> str | None:
    """The install profile (role) a node spec payload is bound to.

    Reads the field the same way node_controller.node_views() reads labels
    and taints, off the spec block of the stored node object.

    Args:
        node_payload: A node object payload from store.read_spec(), or None
            when the object is missing or unreadable.

    Returns:
        The role name, or None when the payload is absent, malformed, or
        carries no role. An unbound node is the normal state before every
        node is backfilled and must never read as an error.
    """
    if not isinstance(node_payload, dict):
        return None
    spec = node_payload.get("spec")
    if not isinstance(spec, dict):
        return None
    role = spec.get("role")
    if not isinstance(role, str):
        return None
    return role.strip() or None


def _forbidden_units(role: str, manifests: Path | None) -> frozenset[str] | None:
    """The role's units.mustNot set, or None when it cannot be established.

    None and an empty set are deliberately different answers: None means
    "no manifest spoke", empty means "the manifest spoke and forbids
    nothing". Only the caller's degrade path collapses them.
    """
    # The role comes off a synced spec file, so it is untrusted input for a
    # path join; valid_name() is what keeps ../ out of the manifest lookup.
    if not valid_name(role):
        return None
    directory = manifest_dir() if manifests is None else manifests
    if directory is None:
        return None
    try:
        doc = json.loads((directory / f"{role}.json").read_text(encoding="utf-8"))
        spec = normalize_profile_spec(doc["spec"])
    except (OSError, ValueError, KeyError, TypeError):
        # Missing file, bad JSON, ProfileSpecError (a ValueError) or a
        # payload without a spec block: all "the manifest did not speak".
        return None
    return frozenset(spec["units"]["mustNot"])


def unit_allowed(role: str | None, unit: str, *, manifests: Path | None = None) -> bool:
    """True when a node of this role may run this unit.

    Only `units.mustNot` denies. Everything a manifest does not mention is
    permitted, deliberately: `unexpected` is the manifest lagging reality
    (profile_doctor grades it info, not error), and refusing a service over
    a stale manifest would turn documentation debt into an outage.

    Args:
        role: The node's install profile name, or None/"" when unbound.
        unit: The systemd unit or container name from a service spec.
        manifests: Manifest directory override, mainly for tests. None
            resolves through manifest_dir().

    Returns:
        False only when the manifest for that role was read, validated, and
        lists this unit under units.mustNot. True in every other case,
        including an unknown role and unreadable manifests.
    """
    if not role or not unit:
        return True
    forbidden = _forbidden_units(role, manifests)
    if forbidden is None:
        return True
    return unit not in forbidden
