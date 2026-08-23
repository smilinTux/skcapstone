#!/usr/bin/env python3
"""Generate a node's unit disposition table mechanically.

Epic 3bbf39ea, cards 5ad840ac (.41 markdown table) and bf83eed2 (.100 JSON
plan plus a revert script). One generator, because the two nodes ask the same
question and a hand-written second copy is how the two answers drift apart.

The point is that Chef reviews a FILLED-IN table rather than a blank one. So
the classification lives in a declarative rules table below, where the
reasoning is data that can be read and argued with, not control flow buried
in a function.

Inputs are checked-in JSON inventories under docs/fleet/inventories/,
collected read-only. There is NO ssh in this script: collection already
happened, and keeping it out means the test path and the live path are the
same path.

Node identity gotcha: .41 reports as fleet node `node-41`, not
`node-cbrd21-laptop12thgenintelcore`, because its sknoded.service sets
SKFLEET_NODE=node-41, which overrides paths.self_node_name(). The control
node is `node-noroc2027`, NOT `node-158`. That mismatch is also why
admission.PRESETS keys are dead.

Usage:
    python scripts/fleet/gen-node-disposition.py --node node-41 \\
        --out docs/fleet/node-41-disposition.md

    python scripts/fleet/gen-node-disposition.py --node node-100 \\
        --out docs/fleet/node-100-disposition.md \\
        --json-out docs/fleet/node-100-unit-disposition.json \\
        --revert-out scripts/fleet/dot100-slim-revert.sh
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INVENTORY_DIR = REPO / "docs" / "fleet" / "inventories"

#: The control node, used for the present-on-control column. Derived from
#: hostname by paths.self_node_name(), which is why it is not "node-158".
REFERENCE_NODE = "node-noroc2027"

#: Checked BEFORE the baseline patterns, so a unit that matters to the fleet
#: is never filtered out by a broad distro glob. systemd-oomd is the clearest
#: case: it matches `systemd-*` but it is the unit behind the .41 freezes, so
#: it has to stay in the review.
ALWAYS_IN_SCOPE = (
    "systemd-oomd.service",
    "k3s*",
    "ollama.service",
    "docker.service",
    "docker.socket",
    "containerd.service",
    "netbird.service",
    "meshagent.service",
    "tailscaled.service",
    "sshd.service",
    "ssh.service",
    "firewalld.service",
    "libvirtd*",
    # Deliberately NOT `lxd*`: lxd-installer.socket is Ubuntu's install-on-
    # demand shim, distro baseline on every box, and a broad glob would drag
    # it into scope on a node that has no container story at all.
    "lxd.service",
    "lxd.socket",
    "lxd-agent*",
    "lxc.service",
    "sk*",
    "cloudflared*",
    "livekit*",
    "syncthing.service",
    "gtd-*",
    "kodi-*",
    "direnv-*",
)

#: Distro and desktop baseline. Out of scope for the node-roles epic: these
#: are the OS being an OS, and the profile layer takes no position on them.
#: fnmatch patterns, checked before any keep or disable rule.
OS_BASELINE = (
    # Snap mount units. The single largest source of noise on .41 (58 of its
    # 108 enabled units), and pure distro plumbing.
    "var-lib-snapd-*",
    "snap-*",
    # Desktop and workstation services on a box that is also someone's laptop.
    "ModemManager.service",
    "NetworkManager*",
    "add-autologin-group.service",
    "avahi-daemon*",
    "bluetooth.service",
    "cronie.service",
    "cups*",
    "haveged.service",
    "iptables.service",
    "lightdm.service",
    "gdm*",
    "nfs-readahead.timer",
    "pamac-*",
    "piavpn.service",
    "teamviewerd.service",
    "thermal-tune.service",
    "virtlockd*",
    "virtlogd*",
    "gnome-keyring-daemon.socket",
    "docker-desktop.service",
    "apparmor.service",
    "apport*",
    "apt-daily*",
    "blk-availability.service",
    "cloud-config.service",
    "cloud-final.service",
    "cloud-init*",
    "console-setup.service",
    "dm-event.socket",
    "dpkg-db-backup.timer",
    "e2scrub*",
    "finalrd.service",
    "fsidd.service",
    "fstrim.timer",
    "getty@.service",
    "grub-*",
    "iscsid.socket",
    "keyboard-setup.service",
    "lvm2-*",
    "lxd-installer.socket",
    "mdcheck*",
    "mdmonitor*",
    "motd-news.timer",
    "multipathd*",
    "networkd-dispatcher.service",
    "open-iscsi.service",
    "pollinate.service",
    "setvtrgb.service",
    "snapd*",
    "ssh.socket",
    "systemd-*",
    "ubuntu-fan.service",
    "unattended-upgrades.service",
    # Desktop session plumbing in the user scope.
    "dirmngr.socket",
    "gpg-agent*.socket",
    "keyboxd.socket",
    "launchpadlib-cache-clean.timer",
    "pk-debconf-helper.socket",
    "p11-kit*",
    "gcr-*",
    "dconf.service",
    "at-spi-*",
    "xdg-*",
    "pipewire*",
    "wireplumber*",
    "pulseaudio*",
    "gvfs*",
    "evolution*",
    "obex.service",
    "speech-dispatcher*",
    "snap.*",
)

#: Hardware and platform units that are baseline for the box they sit on.
HARDWARE_BASELINE = (
    "nvidia-*",
    "gpu-manager.service",
    "thermald.service",
    "power-profiles-daemon.service",
    "upower.service",
    "fwupd*",
    "bolt.service",
    "switcheroo-control.service",
)

#: Per-role keep sets. A unit here stays, with the stated reason. Anything
#: not matched by a keep rule and not baseline falls through to the node's
#: default disposition.
KEEP_RULES: dict[str, list[tuple[str, str]]] = {
    "worker-gpu": [
        ("ollama.service", "the fleet embedding endpoint on :11434"),
        ("mxbai-arc.service", "mxbai-embed-large on the Vulkan iGPU, :11438"),
        ("skai-beellama.service", "serves ornith-1.0-9b on :8082, the fleet sk-default"),
        ("skai-beellama-restart.timer", "keeps the sk-default server healthy"),
        ("comfyui.service", "image generation on :8188"),
        ("comfyui-model-update.timer", "keeps comfyui models current"),
        ("f5-tts.service", "voice synthesis on :18796"),
        ("whisper-stt.service", "speech to text on :18794"),
        ("qwen3-arc.service", "qwen3.5:4b on :8085"),
        ("sovereign-orchestrator.service", "call orchestration on :18801"),
        ("tailscaled.service", "the only reachable path to this node"),
        (
            "syncthing.service",
            "STAYS, but its folder set shrinks to skfleet-control only "
            "(card ddb2a02f); it is also required to unshare the sovereign "
            "folder in an orderly way rather than by deletion",
        ),
        (
            "nfs-server.service",
            "EVIDENCE-BACKED: exportfs shows /srv/comfyui exported rw to "
            "192.168.0.0/24 and comfyui writes to /mnt/comfyui-nfs/output. "
            "It is serving something real",
        ),
        ("nfs-blkmap.service", "nfs-server dependency"),
        ("nfs-client.target", "nfs-server dependency"),
        ("remote-fs.target", "mounts /mnt/comfyui-nfs that comfyui writes into"),
        ("rpcbind.service", "nfs-server dependency"),
        ("rpcbind.socket", "nfs-server dependency"),
        (
            "docker.service",
            "EVIDENCE-BACKED: docker ps shows the frigate NVR healthy and up "
            "15 hours on :5000/:8554-8555/:8971. Out of SK scope but "
            "load-bearing for the household; not this epic's to remove",
        ),
        ("docker.socket", "docker.service dependency"),
        ("containerd.service", "docker.service dependency"),
        ("syncthing-resume.service", "resumes syncthing after suspend"),
    ],
}

#: Units explicitly retired, with the evidence for the call.
DISABLE_RULES: dict[str, list[tuple[str, str]]] = {
    "worker-gpu": [
        (
            "skvoice.service",
            "EVIDENCE-BACKED: enabled but ActiveState=inactive, SubState=dead. "
            "An enabled unit that never runs is exactly the half-alive "
            "duplicate class behind the comms pileups. Retire it",
        ),
        (
            "session-migration.service",
            "Ubuntu session migration one-shot; a headless worker has no "
            "desktop session to migrate",
        ),
    ],
}

#: What a unit gets when no rule matches. worker-gpu is a closed profile
#: (anything unlisted is out of profile); builder-standby is not being
#: slimmed by this card, so unmatched units are proposed as standby and the
#: rationale column is filled by the next subtask (card 468f74a6).
DEFAULT_DISPOSITION = {"worker-gpu": "disable", "builder-standby": "standby"}

NODE_ROLES = {"node-100": "worker-gpu", "node-41": "builder-standby"}

#: Raw command output backing the judgement calls, captured read-only
#: 2026-08-14. Verbatim, so a reviewer checks the evidence rather than the
#: summary of it.
EVIDENCE: dict[str, str] = {
    "node-100": """\
## Evidence for the three judgement calls

### `nfs-server.service`: KEEP. It is serving something.

```console
$ exportfs -v
/srv/comfyui  192.168.0.0/24(sync,wdelay,hide,no_subtree_check,sec=sys,rw,
                             secure,no_root_squash,no_all_squash)
EOEXPORTS     <world>(sync,wdelay,hide,no_subtree_check,sec=sys,ro,secure,
                      root_squash,no_all_squash)
```

`/srv/comfyui` is exported read-write to the whole LAN, and `comfyui.service`
runs with `--output-directory /mnt/comfyui-nfs/output`. The export is live and
load-bearing, so nfs-server stays along with `rpcbind`, `nfs-blkmap`,
`nfs-client.target` and `remote-fs.target`.

🔴 **Two defects found in that output, neither in scope for this card:**

1. **`EOEXPORTS` is being exported to `<world>`.** That is a heredoc
   terminator that leaked into `/etc/exports` and is now a real export entry.
   `exportfs` even warns about it: `No host name given with EOEXPORTS`. It is
   read-only and root-squashed, so the exposure is limited, but it is an
   unintended world-facing NFS export on a box reachable from the LAN.
2. **`/srv/comfyui` is exported `no_root_squash`** to `192.168.0.0/24`. Any
   LAN host that can mount it writes as root.

Both belong on a new security card, not on the slim.

### `docker.service`: KEEP. It is running the household NVR.

```console
$ docker ps
CONTAINER ID   IMAGE                                     STATUS                  NAMES
064037aa4162   ghcr.io/blakeblackshear/frigate:stable    Up 15 hours (healthy)   frigate
```

Ports 5000, 8554-8555 (tcp and udp) and 8971. `docker ps -a` shows no other
containers, running or stopped. Frigate is outside SK scope, but it is live
and this epic has no mandate to take down Chef's cameras. Keep, with
`docker.socket` and `containerd.service`.

### `skvoice.service`: DISABLE. Enabled but dead.

```console
$ systemctl --user show skvoice.service -p ActiveState -p SubState --value
inactive
dead
```

Enabled at boot, never running. That is exactly the half-alive duplicate
class behind the comms pileups and oomd freezes. Retire it; the generated
revert script re-enables it in one line if that call is wrong.

### `syncthing.service`: KEEP, with a shrunken folder set.

It stays because it is the only orderly way to **unshare** the sovereign
folder (card `3118769c`); deleting `~/.skcapstone` on .100 while it is still
shared would propagate the delete to .158, .41 and noroc2027. After the split
its folder set is exactly one entry: `skfleet-control`. The folder it loses
is `skcapstone-sync` (`SKCapstone Sovereign`, 5.0G on this node across 29
agent directories). See `docs/fleet/control-bus-folder.md`.

### No cron surface

```console
$ which crontab
(no output)
```

.100 has no crontab binary installed, so there is nothing to remove there.
""",
    "node-41": """\
## Rationale column

Deliberately blank. Card `5ad840ac` generates the filled-in table; card
`468f74a6` fills the rationale and flags the two Chef-only questions
(`skchat-daemon-jarvis` load-bearing? `cloudflared-fed` an intentional second
ingress?). A generator that guessed the rationale would be the opinion this
card exists to avoid.

Nothing on .41 is proposed for removal here. Unmatched units default to
`standby`, never `disable`.
""",
}

NODE_LABELS = {"node-100": ".100", "node-41": ".41", "node-noroc2027": ".158 (control)"}


def _match(unit: str, patterns) -> bool:
    return any(fnmatch.fnmatch(unit, pattern) for pattern in patterns)


def _rule_for(unit: str, rules: list[tuple[str, str]]) -> str | None:
    for pattern, reason in rules:
        if fnmatch.fnmatch(unit, pattern):
            return reason
    return None


def load_inventory(node: str, scope: str) -> dict[str, str]:
    """Read one checked-in inventory, or raise a useful error."""
    path = INVENTORY_DIR / f"{node}-{scope}-units.json"
    if not path.exists():
        raise SystemExit(
            f"missing inventory: {path}\n"
            "Collect it read-only with:\n"
            f"  systemctl {'--user ' if scope == 'user' else ''}"
            "list-unit-files --state=enabled --no-legend"
        )
    return json.loads(path.read_text(encoding="utf-8"))["units"]


def classify(unit: str, scope: str, role: str) -> tuple[str, str]:
    """Return (disposition, rationale) for one unit.

    Order matters. ALWAYS_IN_SCOPE wins over the baseline globs so a
    fleet-relevant unit is never filtered out by a broad pattern, and
    baseline is checked before the fallthrough so a distro unit can never be
    proposed for removal by default.
    """
    if _match(unit, ALWAYS_IN_SCOPE):
        reason = _rule_for(unit, DISABLE_RULES.get(role, []))
        if reason:
            return "disable", reason
        reason = _rule_for(unit, KEEP_RULES.get(role, []))
        if reason:
            return "keep", reason
        return DEFAULT_DISPOSITION.get(role, "standby"), ""
    if _match(unit, OS_BASELINE):
        return "out-of-scope", "distro or desktop baseline"
    if _match(unit, HARDWARE_BASELINE):
        return "out-of-scope", "hardware and platform baseline"
    reason = _rule_for(unit, DISABLE_RULES.get(role, []))
    if reason:
        return "disable", reason
    reason = _rule_for(unit, KEEP_RULES.get(role, []))
    if reason:
        return "keep", reason
    return DEFAULT_DISPOSITION.get(role, "standby"), ""


def build_rows(node: str, role: str, reference: dict[str, str]) -> list[dict]:
    """One row per enabled unit across both scopes, sorted deterministically."""
    rows = []
    for scope in ("user", "system"):
        for unit in load_inventory(node, scope):
            disposition, rationale = classify(unit, scope, role)
            rows.append(
                {
                    "unit": unit,
                    "scope": scope,
                    "present_on_control": "yes" if unit in reference else "no",
                    "disposition": disposition,
                    "rationale": rationale,
                }
            )
    return sorted(rows, key=lambda r: (r["scope"], r["unit"]))


def render_markdown(node: str, role: str, rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["disposition"]] = counts.get(row["disposition"], 0) + 1
    label = NODE_LABELS.get(node, node)
    lines = [
        f"# {label} unit disposition (`{node}`, role `{role}`)",
        "",
        "Generated by `scripts/fleet/gen-node-disposition.py`. Do not hand-edit:",
        "regenerate it. Epic `3bbf39ea`.",
        "",
        "Collected read-only. Nothing on the node was changed to produce this.",
        "",
        "| disposition | count |",
        "|---|---|",
    ]
    for name in sorted(counts):
        lines.append(f"| `{name}` | {counts[name]} |")
    lines += [
        f"| **total** | **{len(rows)}** |",
        "",
        "`present-on-control` compares against the control node "
        f"(`{REFERENCE_NODE}`, which is what `paths.self_node_name()` derives "
        "from the hostname; there is no `node-158`).",
        "",
        "| unit | scope | present-on-control | disposition | rationale |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| `{row['unit']}` | {row['scope']} | {row['present_on_control']} "
            f"| {row['disposition']} | {row['rationale']} |"
        )
    lines.append("")
    evidence = EVIDENCE.get(node)
    if evidence:
        lines.append(evidence)
    return "\n".join(lines)


def render_revert(node: str, rows: list[dict]) -> str:
    """A revert script whose enable lines mirror the plan's disable lines."""
    disabled = sorted((r["scope"], r["unit"]) for r in rows if r["disposition"] == "disable")
    lines = [
        "#!/usr/bin/env bash",
        f"# Revert the {node} unit slim. Generated by",
        "# scripts/fleet/gen-node-disposition.py. Do not hand-edit.",
        "#",
        "# Re-enables exactly the units the disposition plan disables, and",
        "# nothing else. Every step in this epic owes a documented revert.",
        "set -euo pipefail",
        "",
    ]
    for scope, unit in disabled:
        flag = "--user " if scope == "user" else ""
        lines.append(f"systemctl {flag}enable {unit}")
    lines += ["", f'echo "reverted {len(disabled)} unit(s) on {node}"', ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", required=True, help="fleet node name, e.g. node-41")
    parser.add_argument("--out", required=True, help="markdown output path")
    parser.add_argument("--json-out", default=None, help="machine-readable plan path")
    parser.add_argument("--revert-out", default=None, help="revert script path")
    parser.add_argument("--reference", default=REFERENCE_NODE, help="control node name")
    args = parser.parse_args()

    role = NODE_ROLES.get(args.node)
    if role is None:
        raise SystemExit(f"unknown node {args.node!r} (known: {sorted(NODE_ROLES)})")

    reference: dict[str, str] = {}
    for scope in ("user", "system"):
        reference.update(load_inventory(args.reference, scope))

    rows = build_rows(args.node, role, reference)

    # Assert the row count rather than leaving it to be eyeballed.
    collected = sum(len(load_inventory(args.node, scope)) for scope in ("user", "system"))
    if len(rows) != collected:
        raise SystemExit(f"row count {len(rows)} != collected unit count {collected}")
    for row in rows:
        if not row["unit"] or not row["scope"] or not row["present_on_control"]:
            raise SystemExit(f"incomplete row: {row}")

    # Nothing gets proposed for removal without a stated reason. A silent
    # fallthrough into `disable` is how a load-bearing unit gets switched off
    # because no rule happened to name it.
    unexplained = [r["unit"] for r in rows if r["disposition"] == "disable" and not r["rationale"]]
    if unexplained:
        raise SystemExit(
            f"{len(unexplained)} unit(s) proposed for disable with no rationale: "
            f"{unexplained}. Add a rule naming them, or let them fall to baseline."
        )

    Path(args.out).write_text(render_markdown(args.node, role, rows), encoding="utf-8")
    print(f"wrote {args.out} ({len(rows)} rows, all {collected} enabled units)")

    if args.json_out:
        payload = {
            "node": args.node,
            "role": role,
            "referenceNode": args.reference,
            "unitCount": len(rows),
            "units": rows,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.json_out}")

    if args.revert_out:
        path = Path(args.revert_out)
        path.write_text(render_revert(args.node, rows), encoding="utf-8")
        path.chmod(0o755)
        planned = sorted(r["unit"] for r in rows if r["disposition"] == "disable")
        reverted = sorted(
            line.split()[-1]
            for line in path.read_text().splitlines()
            if line.startswith("systemctl")
        )
        if planned != reverted:
            raise SystemExit(f"revert drift: plan disables {planned}, revert enables {reverted}")
        print(f"wrote {args.revert_out} ({len(planned)} unit(s), mirrors the plan exactly)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
