"""skfleet-clients ring: roll pinned clients and rendered configs host by host.

Modelled on scripts/fleet/skfleet-auto-rollout.sh: one authority host
(chiap08), a lock, canary first, ssh probe and rollout-busy-locks skips, halt at
the first failed gate with ROLLED/HALTED skmail and a halt streak escalation.
Design: docs/fleet/CLIENTS.md.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

from . import clients as spec_mod

AGENT_REMOTE = "~/.local/lib/skfleet/clients-agent.py"
DEFAULT_REPO = "~/deploy/skgateway-config-chi"
HALT_RETRY_SECONDS = 3600
HALT_ESCALATE = 5


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / "skfleet-clients"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg, flush=True)


def agent_source() -> bytes:
    return resources.files("skcapstone.data").joinpath("skfleet-clients-agent.py").read_bytes()


def is_local(host: str) -> bool:
    short = socket.gethostname().split(".")[0]
    return host in (short, "localhost")


SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def call_agent(host: str, action: str, payload: dict, *, timeout: float = 1800) -> dict:
    """Run the host agent and return its JSON answer (errors become {'error'})."""
    src = agent_source()
    data = json.dumps(payload).encode()
    if is_local(host):
        path = Path(os.path.expanduser(AGENT_REMOTE))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(src)
        cmd = ["python3", str(path), action]
        stdin = data
    else:
        remote = (
            f"mkdir -p ~/.local/lib/skfleet && head -c {len(src)} > {AGENT_REMOTE}.tmp"
            f" && mv {AGENT_REMOTE}.tmp {AGENT_REMOTE}"
            f" && exec python3 {AGENT_REMOTE} {shlex.quote(action)}"
        )
        cmd = ["ssh", *SSH_OPTS, host, remote]
        stdin = src + data
    try:
        res = subprocess.run(cmd, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"{action} timed out after {timeout}s"}
    out = res.stdout.decode(errors="replace").strip().splitlines()
    try:
        return json.loads(out[-1])
    except (IndexError, ValueError):
        err = res.stderr.decode(errors="replace").strip()[-300:]
        return {"error": f"{action} rc {res.returncode}: {err or 'no output'}"}


def reachable(host: str) -> bool:
    if is_local(host):
        return True
    res = subprocess.run(
        ["ssh", *SSH_OPTS, "-o", "ConnectTimeout=5", host, "true"], capture_output=True, timeout=30
    )
    return res.returncode == 0


BUSY_CHECK = r'''f="$HOME/.config/skfleet/rollout-busy-locks"
[ -r "$f" ] || exit 0
while read -r g; do
  case "$g" in ""|"#"*) continue ;; esac
  for l in $g; do
    [ -e "$l" ] || continue
    flock -n "$l" true 2>/dev/null || { printf "%s\n" "$l"; exit 0; }
  done
done < "$f"'''


def held_busy_lock(host: str) -> str:
    """Same contract as skfleet-auto-rollout: first held lock, else ''."""
    cmd = ["bash", "-c", BUSY_CHECK] if is_local(host) else ["ssh", *SSH_OPTS, host, BUSY_CHECK]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ""
    return res.stdout.strip()


def notify(recipients: tuple[str, ...], subject: str, body: str) -> None:
    skmail = os.environ.get("SKMAIL_BIN", "skmail")
    for to in recipients:
        try:
            subprocess.run(
                [skmail, "send", "skfleet-clients", to, "normal", subject, body],
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


# ------------------------------------------------------------------ state


@dataclass
class RingState:
    root: Path = field(default_factory=state_dir)

    def _read(self, name: str) -> dict:
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, name: str, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / f".{name}.tmp"
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.root / name)

    def host(self, host: str) -> dict:
        return self._read(f"host-{host}.json")

    def set_host(self, host: str, data: dict) -> None:
        self._write(f"host-{host}.json", data)

    @property
    def last_render(self) -> dict:
        return self._read("last-render.json")

    def set_last_render(self, data: dict) -> None:
        self._write("last-render.json", data)

    @property
    def halt(self) -> dict:
        return self._read("halt.json")

    def set_halt(self, data: dict) -> None:
        self._write("halt.json", data)

    def clear_halt(self) -> None:
        (self.root / "halt.json").unlink(missing_ok=True)


def pins_hash(desired: dict) -> str:
    return spec_mod.state_hash({"clients": desired["clients"], "root": desired["install_root"]})


# ------------------------------------------------------------------ ring


@dataclass
class Outcome:
    status: str  # noop | rolled | partial | halted | refused
    rolled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    halted_at: str = ""
    reason: str = ""


def roll_host(host: str, desired: dict, *, smoke: bool, agent: Callable = call_agent) -> str:
    """Apply, gate (and smoke on the canary). Returns '' on success, else why."""
    applied = agent(host, "apply", desired)
    if applied.get("error") or applied.get("errors"):
        agent(host, "rollback", {})
        return "apply: " + "; ".join([applied.get("error", "")] + applied.get("errors", [])).strip(
            "; "
        )
    gated = agent(host, "gate", desired)
    if gated.get("error") or not gated.get("ok"):
        agent(host, "rollback", {})
        return "gate: " + (gated.get("error") or "; ".join(gated.get("failures", [])))
    if smoke:
        smoked = agent(host, "smoke", desired, timeout=1500)
        if smoked.get("error") or not smoked.get("ok"):
            agent(host, "rollback", {})
            bad = {
                k: v.get("tail", "")
                for k, v in (smoked.get("results") or {}).items()
                if not v.get("ok")
            }
            return "smoke: " + (smoked.get("error") or json.dumps(bad)[:600])
    agent(host, "prune", desired)
    return ""


def tick(
    spec: spec_mod.Spec,
    rows: list[dict],
    *,
    hosts: list[str] | None = None,
    state: RingState | None = None,
    agent: Callable = call_agent,
    probe: Callable[[str], bool] = reachable,
    busy: Callable[[str], str] = held_busy_lock,
    mail: Callable[[tuple, str, str], None] = notify,
    force: bool = False,
    now: Callable[[], float] = time.time,
) -> Outcome:
    state = state or RingState()
    ring = [h for h in spec.ring if hosts is None or h in hosts]
    desired = {h: spec_mod.render_host(spec, h, rows) for h in ring}
    target = spec_mod.state_hash({h: spec_mod.state_hash(d) for h, d in desired.items()})
    pending = [h for h in ring if state.host(h).get("hash") != spec_mod.state_hash(desired[h])]
    if not pending:
        return Outcome("noop")

    halt = state.halt
    if (
        not force
        and halt.get("target") == target
        and now() - halt.get("at", 0) < HALT_RETRY_SECONDS
    ):
        return Outcome(
            "halted", halted_at=halt.get("host", ""), reason="waiting to retry halted state"
        )

    canary = spec.canary
    last = state.last_render.get(canary)
    drop = spec_mod.model_drop(last, desired.get(canary, {}))
    if not force and drop > spec.max_model_drop:
        reason = (
            f"render would drop {drop:.0%} of models at once (limit {spec.max_model_drop:.0%}); "
            "check the gateway catalog, then run `skcapstone fleet clients apply --force`"
        )
        return _halt(spec, state, target, canary, reason, [], mail, now)

    rolled: list[str] = []
    skipped: list[str] = []
    for host in ring:
        want = desired[host]
        if host not in pending:
            rolled.append(host)
            continue
        if not probe(host):
            log(f"{host} unreachable; skipped, ring continues")
            skipped.append(host)
            continue
        held = busy(host)
        if held:
            log(f"{host} busy ({held} held); skipped, ring continues")
            skipped.append(host)
            continue
        prev = state.host(host)
        smoke = host == canary and prev.get("pins") != pins_hash(want)
        want = dict(want, state_hash=spec_mod.state_hash(want))
        why = roll_host(host, want, smoke=smoke, agent=agent)
        if why:
            return _halt(spec, state, target, host, why, rolled, mail, now)
        state.set_host(host, {"hash": want["state_hash"], "pins": pins_hash(want), "at": now()})
        state.set_last_render({**state.last_render, host: desired[host]})
        rolled.append(host)
        log(f"{host} rolled")

    if skipped:
        log(f"PARTIAL: rolled {rolled}; retried next tick: {skipped}")
        return Outcome("partial", rolled=rolled, skipped=skipped)
    state.clear_halt()
    versions = ", ".join(f"{c.name} {c.version}" for c in spec.clients.values())
    mail(
        spec.notify,
        "skfleet-clients ROLLED",
        f"{versions}; configs from {spec.gateway} -> {' '.join(rolled)} (gates passed)",
    )
    log(f"ROLLED to {rolled}")
    return Outcome("rolled", rolled=rolled)


def _halt(spec, state, target, host, why, rolled, mail, now) -> Outcome:
    prev = state.halt
    streak = prev.get("streak", 0) + 1 if prev.get("target") == target else 1
    state.set_halt(
        {
            "target": target,
            "host": host,
            "reason": why,
            "at": now(),
            "streak": streak,
            "since": prev.get("since", now()) if prev.get("target") == target else now(),
        }
    )
    text = (
        f"skfleet-clients HALTED at {host}: {why}. "
        f"Rolled before it: {' '.join(rolled) or 'none'}; "
        f"later hosts keep their previous clients and configs. Journal: "
        f"journalctl --user -u skfleet-clients on {socket.gethostname()}"
    )
    log("HALT " + text)
    if streak == 1:
        mail(spec.notify, f"skfleet-clients HALTED at {host}", text)
    elif streak == HALT_ESCALATE:
        mail(
            spec.notify,
            f"ESCALATE skfleet-clients stuck at {host}",
            f"{streak} halts in a row. {text}",
        )
    return Outcome("halted", rolled=rolled, halted_at=host, reason=why)


# ------------------------------------------------------------------ config repo


def sync_repo(repo: Path) -> str:
    """Fast-forward the deploy checkout to origin/main. Returns '' or why not."""
    git = ["git", "-C", str(repo)]
    if subprocess.run([*git, "fetch", "-q", "origin", "main"], capture_output=True).returncode:
        return "fetch failed"
    head = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    tgt = subprocess.run(
        [*git, "rev-parse", "origin/main"], capture_output=True, text=True
    ).stdout.strip()
    if head == tgt:
        return ""
    if subprocess.run([*git, "merge-base", "--is-ancestor", head, tgt]).returncode:
        return f"deploy checkout {head[:8]} is not an ancestor of origin/main {tgt[:8]}"
    if subprocess.run([*git, "merge", "-q", "--ff-only", tgt], capture_output=True).returncode:
        return "fast-forward failed"
    return ""


class Lock:
    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def __exit__(self, *exc) -> None:
        if self.fh:
            self.fh.close()


def lock_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return Path(base) / "skfleet-clients.lock"


# ------------------------------------------------------------------ upstream


def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def latest_version(pin: spec_mod.ClientPin) -> tuple[str, str]:
    """(version, sha256) of the newest upstream release for one client."""
    if pin.source == "npm":
        res = subprocess.run(
            ["npm", "view", pin.package, "version"], capture_output=True, text=True, timeout=60
        )
        if res.returncode:
            raise RuntimeError(f"npm view {pin.package} failed")
        return res.stdout.strip(), ""
    repo, asset = pin.upstream.get("github"), pin.upstream.get("asset")
    if not repo or not asset:
        raise RuntimeError(f"{pin.name}: release pins need upstream.github and upstream.asset")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        rel = json.loads(resp.read())
    version = str(rel.get("tag_name", "")).lstrip("v")
    for a in rel.get("assets", []):
        if a.get("name") == asset and str(a.get("digest", "")).startswith("sha256:"):
            return version, a["digest"].split(":", 1)[1]
    raise RuntimeError(f"{pin.name}: release {version} has no sha256 digest for {asset}")


def bump_pins_text(text: str, client: str, version: str, sha256: str) -> str:
    """Rewrite one client's version (and sha256) inside pins.yaml, keeping comments."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if re.match(rf"^  {re.escape(client)}:\s*$", ln))
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i])), len(lines)
    )
    for i in range(start + 1, end):
        if re.match(r"^    version:", lines[i]):
            lines[i] = f"    version: {version}\n"
        elif sha256 and re.match(r"^    sha256:", lines[i]):
            lines[i] = f"    sha256: {sha256}\n"
    return "".join(lines)


def bump_gateway_env(text: str, var: str, version: str) -> str:
    return re.sub(rf"(Environment=\"?{re.escape(var)}=)[^\s\"]+", rf"\g<1>{version}", text)
