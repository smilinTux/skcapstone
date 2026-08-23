#!/usr/bin/env python3
"""Generate the four install-profile manifests from real node inventories.

Epic 3bbf39ea, cards 9731182e (control), 9c2839c2 (builder-standby and
worker-gpu), 21469c38 (observer).

Hand-typing a manifest produces an idealized list that never matches the
node. These are generated from the checked-in inventories under
docs/fleet/inventories/, which were collected read-only, so `allowed`
describes what is really there.

Output: deploy/fleet-objects/profile/*.json (decision card c5ad2471: that is
the manifest home; docs/fleet/profiles.md is the schema reference only).

Usage:
    python scripts/fleet/gen-profile-manifests.py
    python scripts/fleet/gen-profile-manifests.py --check   # verify, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INVENTORY_DIR = REPO / "docs" / "fleet" / "inventories"
OUT_DIR = REPO / "deploy" / "fleet-objects" / "profile"

#: A unit is SK-managed when it carries one of these prefixes. This is the
#: documented rule referenced in each manifest's description: everything else
#: on the box is desktop, distro or third-party, and a role profile takes no
#: position on it.
SK_UNIT_PREFIXES = ("sk", "capauth", "cloud9")

#: Units that are not SK-prefixed but ARE fleet-relevant, so they belong in
#: `allowed` rather than being ignored as noise.
FLEET_ADJACENT = ("syncthing.service", "syncthing-resume.service", "tailscaled.service")

#: Model-serving units. These are the worker's whole job and must never run
#: on the control seat or the standby: they pin a GPU and a lot of RAM.
MODEL_SERVING = [
    "comfyui.service",
    "f5-tts.service",
    "mxbai-arc.service",
    "ollama.service",
    "qwen3-arc.service",
    "skai-beellama.service",
    "whisper-stt.service",
]

#: Control-plane loops. A travelling laptop must never run these: two seats
#: writing the same fleet files is exactly the single-writer violation the
#: store's ownership guard exists to prevent.
CONTROL_PLANE_LOOPS = [
    "skcapstone-dashboard.service",
    "skgateway.service",
    "skoperator.timer",
    "skos-web.service",
]

#: The control seat's defining units. Every name here is asserted to be
#: really enabled on the control node before the manifest is written.
CONTROL_REQUIRED = [
    "capauth-authz.service",
    "skcapstone.service",
    "skgateway.service",
    "sknoded.service",
    "skoperator.timer",
]

#: Desktop, distro and third-party noise. Patterns, not names, so a manifest
#: does not have to be regenerated every time someone installs a snap.
DESKTOP_IGNORE = [
    "anthropic-proxy.service",
    "archive-sessions.timer",
    "at-spi-*",
    "battery-cycle-watch.timer",
    "claude-code-api.service",
    "cloudflared-*",
    "dconf.service",
    "dirmngr.socket",
    "evolution*",
    "filter-chain.service",
    "gcr-*",
    "gnome-keyring-daemon.*",
    "gpg-agent*.socket",
    "gpu-maint-reminder.timer",
    "gvfs*",
    "hermes-*",
    "jarvis-heartbeat.service",
    "keyboxd.socket",
    "kokoro-proxy.service",
    "launchpadlib-cache-clean.timer",
    "livekit-server.service",
    "obex.service",
    "org.freedesktop.*",
    "pipewire*",
    "pk-debconf-helper.socket",
    "pulseaudio*",
    "push-pending.timer",
    "session-migration.service",
    "shadowcopy-*",
    "snap.*",
    "speech-dispatcher*",
    "telegram-catchup.timer",
    "tracker-*",
    "ubuntu-report.path",
    "virtualmic.service",
    "weather-*",
    "wiki-reconcile.timer",
    "wireplumber*",
    "xdg-*",
]

#: Every manifest points back at the decision record that explains WHY the
#: role exists and why its state tier is what it is. A manifest read on its
#: own answers "what", and the ADR is the only place that answers "why".
_ADR_LINK = "Role model and the two orthogonal axes: docs/fleet/adr-node-role-model.md."

_IGNORE_RULE = (
    "SK-managed units are those prefixed sk/capauth/cloud9, plus the "
    "fleet-adjacent syncthing and tailscaled units. Everything else on the "
    "box is desktop, distro or third-party and is listed in unitsIgnore, so "
    "it never reads as drift."
)


def load_units(node: str, scope: str = "user") -> list[str]:
    path = INVENTORY_DIR / f"{node}-{scope}-units.json"
    if not path.exists():
        raise SystemExit(f"missing inventory: {path}")
    return sorted(json.loads(path.read_text(encoding="utf-8"))["units"])


def load_packages(node: str) -> list[str]:
    """SK-namespace packages really installed on a node, from the inventory."""
    path = INVENTORY_DIR / f"{node}-packages.json"
    if not path.exists():
        raise SystemExit(f"missing package inventory: {path}")
    return sorted(json.loads(path.read_text(encoding="utf-8"))["packages"])


#: Sovereign packages a worker must never carry. These are the ones that
#: bring state with them: memory, chat history, comms queues, coordination
#: cards and seeds. A GPU worker serves inference and holds nothing.
SOVEREIGN_PACKAGES = [
    "skchat",
    "skchat-sovereign",
    "skcomm",
    "skcomms",
    "skcoord",
    "skmemory",
    "skseed",
]

#: Non-SK-prefixed units that ARE legitimate worker services.
WORKER_EXTRA_UNITS = ["sovereign-orchestrator.service"]


def sk_units(units: list[str]) -> list[str]:
    return sorted(u for u in units if u.startswith(SK_UNIT_PREFIXES) or u in FLEET_ADJACENT)


def _units_block(allowed: list[str], required: list[str], must_not: list[str]) -> dict:
    """Assemble a units block with mustNot taking precedence over observed.

    `allowed` is generated from what is really enabled on the node, so it can
    legitimately contain something the role forbids. When it does, mustNot
    wins: the unit is subtracted from allowed so the manifest is coherent,
    and the live node then shows up in the drift report as a `forbidden`
    finding, which is the whole point. Legalising it in `allowed` would hide
    exactly the drift the profile exists to catch.
    """
    forbidden = set(must_not)
    return {
        "required": sorted(set(required) - forbidden),
        "allowed": sorted((set(allowed) | set(required)) - forbidden),
        "mustNot": sorted(forbidden),
    }


def _manifest(name: str, spec: dict) -> dict:
    return {"kind": "profile", "name": name, "labels": {"epic": "3bbf39ea"}, "spec": spec}


def build_control() -> dict:
    observed = load_units("node-noroc2027")
    allowed = sk_units(observed)
    missing = [u for u in CONTROL_REQUIRED if u not in observed]
    if missing:
        raise SystemExit(
            f"control manifest would require units that are NOT enabled on the "
            f"control node: {missing}. Fix the list or the node, not the check."
        )
    return _manifest(
        "control",
        {
            "description": (
                "The single control seat (.158, node-noroc2027). Holds the full "
                "sovereign tree and runs the control-plane loops. Changes almost "
                "nothing, which is the point. " + _IGNORE_RULE + " " + _ADR_LINK
            ),
            "units": _units_block(allowed, CONTROL_REQUIRED, MODEL_SERVING),
            "unitsIgnore": sorted(DESKTOP_IGNORE),
            "packages": _units_block(load_packages("node-noroc2027"), ["skcapstone"], []),
            "stateTier": "full-replica",
            "capauthIdentityClass": "operator",
            "syncFolders": ["skcapstone-sync", "skfleet-control"],
        },
    )


def build_builder_standby() -> dict:
    observed = load_units("node-41")
    allowed = sk_units(observed)
    return _manifest(
        "builder-standby",
        {
            "description": (
                "The build toolchain and warm state replica (.41, node-41). Two "
                "copies of the STATE, one copy of each running SERVICE: it holds "
                "a full replica and is the promotion target, but it does not run "
                "the control-plane loops. A laptop that sleeps cannot honor "
                "always-on services, and the half-alive duplicates are the proven "
                "source of comms pileups and outbox floods. " + _IGNORE_RULE + " " + _ADR_LINK
            ),
            "units": _units_block(
                allowed, ["sknoded.service"], sorted(set(CONTROL_PLANE_LOOPS) | set(MODEL_SERVING))
            ),
            "unitsIgnore": sorted(DESKTOP_IGNORE),
            "packages": _units_block(load_packages("node-41"), ["skcapstone"], []),
            "stateTier": "full-replica",
            "capauthIdentityClass": "agent",
            "syncFolders": ["skcapstone-sync", "skfleet-control"],
        },
    )


def build_worker_gpu() -> dict:
    observed = load_units("node-100")
    allowed = sorted(set(sk_units(observed)) | set(MODEL_SERVING) | set(WORKER_EXTRA_UNITS))
    return _manifest(
        "worker-gpu",
        {
            "description": (
                "The GPU worker (.100). Serves inference, holds ZERO sovereign "
                "state. The mustNot list is the load-bearing part: no memory "
                "daemon, no agent runtime, no coordination writer, and no "
                "membership in the sovereign Syncthing folder. The sovereign tree "
                "is 19G, 13G of it agent state, and an outbox incident once "
                "reached 83G. " + _IGNORE_RULE + " " + _ADR_LINK
            ),
            "units": _units_block(
                allowed,
                ["skai-beellama.service"],
                sorted(
                    set(CONTROL_PLANE_LOOPS)
                    | {
                        "skcapstone.service",
                        "skchat-daemon.service",
                        "skcomm-daemon.service",
                        "skcomms-api.service",
                        "skmemory-daemon.service",
                        "skwhisper.service",
                    }
                ),
            ),
            "unitsIgnore": sorted(DESKTOP_IGNORE),
            # allowed is deliberately the SHORT list, not what .100 happens to
            # have: the worker is being slimmed TO this, so the manifest states
            # the target and the drift report names the gap.
            "packages": _units_block(["capauth", "skcapstone"], [], SOVEREIGN_PACKAGES),
            # control-bus, NOT none. The tier says how much state the node
            # holds, and this one holds the fleet store: syncFolders below is
            # exactly ["skfleet-control"]. `none` means holds no SK state at
            # all and runs no node agent, which is the observer. Declaring
            # tier `none` while joining a state folder is self-contradictory,
            # and it disagreed with both docs/fleet/profiles.md and the share
            # matrix in docs/fleet/control-bus-folder.md.
            "stateTier": "control-bus",
            "capauthIdentityClass": "worker",
            "syncFolders": ["skfleet-control"],
        },
    )


def build_observer() -> dict:
    return _manifest(
        "observer",
        {
            "description": (
                "Watched, never installed into (norpv1300, the Proxmox "
                "hypervisor hosting the GPU VM). NO INSTALLATION EVER TARGETS AN "
                "OBSERVER NODE. It is reachable for reporting only, and its "
                "inventory may be collected over ssh rather than by a local "
                "sknoded, which is the ssh-pull fallback in docs/fleet/"
                "control-bus-folder.md. Putting SK software on the box that hosts "
                "the node adds risk for near-zero benefit. This manifest exists so "
                "that 'not managed' is a recorded decision rather than an omission. "
                + _ADR_LINK
            ),
            "units": _units_block(
                [],
                [],
                sorted(
                    set(CONTROL_PLANE_LOOPS)
                    | set(MODEL_SERVING)
                    | {
                        "skcapstone.service",
                        "skchat-daemon.service",
                        "skcomm-daemon.service",
                        "sknoded.service",
                    }
                ),
            ),
            "unitsIgnore": ["*"],
            "packages": {
                "required": [],
                "allowed": [],
                "mustNot": ["capauth", "cloud9", "skcapstone", "skcomms", "skmemory"],
            },
            "stateTier": "none",
            "capauthIdentityClass": "observer",
            "syncFolders": [],
        },
    )


BUILDERS = {
    "control": build_control,
    "builder-standby": build_builder_standby,
    "worker-gpu": build_worker_gpu,
    "observer": build_observer,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    args = parser.parse_args()

    sys.path.insert(0, str(REPO / "src"))
    from skcapstone.fleet.profiles import normalize_profile_spec

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    drift = []
    for name, build in sorted(BUILDERS.items()):
        doc = build()
        normalize_profile_spec(doc["spec"])  # refuse to emit anything invalid
        text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
        path = OUT_DIR / f"{name}.json"
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != text:
                drift.append(name)
            continue
        path.write_text(text, encoding="utf-8")
        spec = doc["spec"]
        print(
            f"wrote {path.relative_to(REPO)}  "
            f"tier={spec['stateTier']:12} class={spec['capauthIdentityClass']:8} "
            f"required={len(spec['units']['required'])} "
            f"allowed={len(spec['units']['allowed'])} "
            f"mustNot={len(spec['units']['mustNot'])}"
        )
    if args.check:
        if drift:
            print(f"manifests differ from the generator: {drift}")
            return 1
        print("all four manifests match the generator")
    return 0


if __name__ == "__main__":
    sys.exit(main())
