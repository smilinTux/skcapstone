#!/usr/bin/env python3
"""Collect one day of SKRSI evidence for one node into a private skrsi instance repo.

WHAT IT DOES
    Reads the instance's ``cluster.yaml``, finds this node and its declared
    ``readers``, runs the PINNED framework's ``baseline_<reader>.py`` scripts
    (``framework/scripts``) for a one-day window, and writes the aggregates to
    ``evidence/<node>/<YYYY-MM-DD>.json``. It then runs the framework's
    ``validate_instance.py`` and, with ``--push``, commits only that file and
    pushes it.

    The instance repo decides what is collected; this script holds no node,
    service or estate names. Source locations come from flags or environment
    variables (see ``SOURCES``), with defaults under ``$HOME``.

RULES IT KEEPS
    * Missing data is missingness, never zero: a reader whose source is absent
      or whose script fails is recorded with ``status`` ``missing`` or
      ``error`` and no numbers.
    * Append only: an existing evidence file for the date is never rewritten
      (exit 0, nothing to do).
    * One node writes only its own directory, so nodes never conflict.
    * Fail closed: if validation fails, the new file is removed and the run
      exits non-zero without committing.
    * Aggregates only. The framework scripts emit counts and percentiles,
      never bodies, prompts or credentials.

THE RESOURCES READER
    ``resources`` is built in (no framework script): per unit in the node-local
    allowlist ``SKRSI_RESOURCE_UNITS`` (``[user:|system:]name[.service]``,
    comma or space separated, user scope by default) it reads, from systemd and
    the unit's cgroup, CPU core-percent (``CPUUsageNSec`` delta since the last
    collection of the same invocation, else since the unit started), resident
    memory (sum of ``VmRSS`` over the cgroup's processes, a point sample),
    cgroup memory now and peak, and the kills and restarts journald recorded
    for the evidence day (counted, never copied). Plus the lowest disk free
    percentage over ``SKRSI_RESOURCE_MOUNTS`` (default ``/`` and ``$HOME``) and,
    per syncthing folder id in ``SKRSI_SYNC_FOLDERS``, its global and
    out-of-sync file counts from the local REST API. The last CPU sample is kept
    in ``$SKRSI_STATE_DIR/resources-state.json``. A unit that is not active, or
    a value that cannot be read, is missing or null, never zero. The record
    shape is the one skrsi ``scripts/resources_metrics.py`` reads.

BOUNDARY OF AUTHORITY
    Observation only. It never approves, merges or deploys anything. Pushing
    needs a branch rule that lets the pushing account write ``evidence/**``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

HOME = Path.home()

# reader -> (env var, default path). The path must exist or the reader is missing.
SOURCES = {
    "cron": ("SKRSI_CRON_LEDGER", HOME / ".skcapstone/logs/cron-ledger.jsonl"),
    "cardstore": ("SKRSI_CARD_EVENTS", HOME / ".skcapstone/coordination/card_events"),
    "gateway": ("SKRSI_GATEWAY_DB", HOME / "skworld-deploy/skgateway/data/metrics.db"),
    "sessions": ("SKRSI_SESSIONS_DIR", HOME / ".claude/projects"),
}
SHARE_MODEL_ENV = "SKRSI_SHARE_MODEL"
GH_LIMIT = 100
TIMEOUT_S = 600


RESOURCE_UNITS_ENV = "SKRSI_RESOURCE_UNITS"
RESOURCE_MOUNTS_ENV = "SKRSI_RESOURCE_MOUNTS"
SYNC_FOLDERS_ENV = "SKRSI_SYNC_FOLDERS"
SYNCTHING_CONFIG_ENV = "SKRSI_SYNCTHING_CONFIG"
STATE_DIR_ENV = "SKRSI_STATE_DIR"
CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")
SYNCTHING_CONFIGS = (
    HOME / ".local/state/syncthing/config.xml",
    HOME / ".config/syncthing/config.xml",
)
SHOW_PROPS = (
    "LoadState,ActiveState,InvocationID,CPUUsageNSec,ActiveEnterTimestampMonotonic,ControlGroup"
)
# journald lines systemd writes about a unit: matched by journalctl, counted, never copied.
KILL_PATTERN = r"code=(killed|dumped), status=(?!15/)"
RESTART_PATTERN = r"Scheduled restart job"
MB = 1024 * 1024


def _cmd(args: list[str]) -> subprocess.CompletedProcess | None:
    """Run a command for the resources reader; None when it cannot run."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _monotonic_us() -> int:
    """CLOCK_MONOTONIC in microseconds, the clock systemd's *Monotonic stamps use."""
    return time.monotonic_ns() // 1000


def _http_json(url: str, api_key: str) -> dict | None:
    """GET a local syncthing REST endpoint; None on any failure."""
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # loopback only, self-signed
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return json.loads(resp.read())
    except (OSError, ValueError):
        return None


def _parse_units(spec: str) -> list[tuple[str, str]]:
    """``[user:|system:]name[.service]`` entries to (scope, unit) pairs, deduplicated, in order."""
    out: list[tuple[str, str]] = []
    for item in re.split(r"[\s,]+", spec.strip()):
        if not item:
            continue
        scope, _, name = item.rpartition(":")
        scope = scope or "user"
        if scope not in ("user", "system"):
            raise SystemExit(f"{RESOURCE_UNITS_ENV}: unknown scope {scope!r}")
        unit = name if "." in name else f"{name}.service"
        if (scope, unit) not in out:
            out.append((scope, unit))
    return out


def _unit_key(scope: str, unit: str) -> str:
    """Evidence key: the unit name without ``.service``, ``system:`` prefix for system units."""
    name = unit[: -len(".service")] if unit.endswith(".service") else unit
    return f"system:{name}" if scope == "system" else name


def _show(scope: str, unit: str) -> dict | None:
    proc = _cmd(
        ["systemctl", *(["--user"] if scope == "user" else []), "show", unit, "-p", SHOW_PROPS]
    )
    if proc is None or proc.returncode != 0:
        return None
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cgroup_memory(cgroup: str) -> dict:
    """rss_mb (VmRSS over the cgroup tree), mem_current_mb, mem_peak_mb; None if unreadable."""
    base = CGROUP_ROOT / cgroup.lstrip("/")
    out: dict = {"rss_mb": None, "mem_current_mb": None, "mem_peak_mb": None}
    for key, name in (("mem_current_mb", "memory.current"), ("mem_peak_mb", "memory.peak")):
        try:
            out[key] = round(int((base / name).read_text().split()[0]) / MB, 1)
        except (OSError, ValueError, IndexError):
            pass
    pids: set[str] = set()
    try:
        for procs in [base / "cgroup.procs", *base.rglob("*/cgroup.procs")]:
            pids |= set(procs.read_text().split())
    except OSError:
        return out
    rss_kb, read_any = 0, False
    for pid in pids:
        try:
            status = (PROC_ROOT / pid / "status").read_text()
        except OSError:
            continue  # exited between listing and reading
        m = re.search(r"^VmRSS:\s+(\d+) kB", status, re.M)
        rss_kb += int(m.group(1)) if m else 0
        read_any = True
    if read_any:
        out["rss_mb"] = round(rss_kb / 1024, 1)
    return out


def _journal_count(scope: str, unit: str, end: str, pattern: str) -> int | None:
    """Journal lines for ``unit`` on day ``end`` (UTC) matching ``pattern``; None if unreadable.

    A unit with no journal entries at all is unreadable rather than quiet: without
    journal access every count would read as zero.
    """
    flag = ["--user"] if scope == "user" else []
    probe = _cmd(["journalctl", *flag, "-u", unit, "-n", "1", "-q", "-o", "cat", "--no-pager"])
    if probe is None or not probe.stdout.strip():
        return None
    day = dt.date.fromisoformat(end)
    proc = _cmd(
        [
            "journalctl",
            *flag,
            "-u",
            unit,
            "-q",
            "-o",
            "cat",
            "--no-pager",
            "--since",
            f"{day} 00:00:00 UTC",
            "--until",
            f"{day + dt.timedelta(days=1)} 00:00:00 UTC",
            "-g",
            pattern,
        ]
    )
    if proc is None or proc.returncode not in (0, 1):  # 1: no match
        return None
    return sum(1 for line in proc.stdout.splitlines() if line.strip())


def _disk() -> dict:
    """Lowest free percentage and free GB over the declared mounts, one reading per filesystem."""
    spec = os.environ.get(RESOURCE_MOUNTS_ENV)
    mounts = (
        [Path(m).expanduser() for m in re.split(r"[\s,]+", spec.strip()) if m]
        if spec
        else [Path("/"), HOME]
    )
    seen, pct, gb = set(), [], []
    for mount in mounts:
        try:
            dev = os.stat(mount).st_dev
            st = os.statvfs(mount)
        except OSError:
            continue
        if dev in seen or not st.f_blocks:
            continue
        seen.add(dev)
        pct.append(100 * st.f_bavail / st.f_blocks)
        gb.append(st.f_bavail * st.f_frsize / 1024**3)
    return {
        "mounts": len(seen),
        "free_pct_min": round(min(pct), 2) if pct else None,
        "free_gb_min": round(min(gb), 1) if gb else None,
    }


def _syncthing_api() -> tuple[str, str] | None:
    """(base URL, API key) of the local syncthing GUI from its config.xml, or None."""
    env = os.environ.get(SYNCTHING_CONFIG_ENV)
    for path in [Path(env).expanduser()] if env else SYNCTHING_CONFIGS:
        try:
            gui = ET.parse(path).getroot().find("gui")
        except (OSError, ET.ParseError):
            continue
        if gui is None or not gui.findtext("apikey"):
            continue
        addr = (gui.findtext("address") or "127.0.0.1:8384").strip()
        host, _, port = addr.rpartition(":")
        if host in ("", "0.0.0.0", "[::]"):
            host = "127.0.0.1"
        scheme = "https" if gui.get("tls") == "true" else "http"
        return f"{scheme}://{host}:{port}", gui.findtext("apikey").strip()
    return None


def _sync(folders: list[str]) -> dict:
    if not folders:
        return {}
    api = _syncthing_api()
    out = {}
    for folder in folders:
        data = _http_json(f"{api[0]}/rest/db/status?folder={folder}", api[1]) if api else None
        if not isinstance(data, dict) or "globalFiles" not in data:
            out[folder] = {"status": "missing"}
            continue
        out[folder] = {
            "status": "ok",
            "files": data.get("globalFiles"),
            "local_files": data.get("localFiles"),
            "need_files": data.get("needFiles"),
        }
    return out


def _resources(end: str) -> dict:
    """The built-in resources reader (see the module docstring)."""
    units = _parse_units(os.environ.get(RESOURCE_UNITS_ENV, ""))
    if not units:
        return {"status": "missing", "reason": f"no unit allowlist ({RESOURCE_UNITS_ENV})"}
    state_path = (
        Path(os.environ.get(STATE_DIR_ENV) or HOME / ".local/state/skrsi").expanduser()
        / "resources-state.json"
    )
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    new_state: dict = {}
    result_units: dict = {}
    for scope, unit in units:
        key = _unit_key(scope, unit)
        show = _show(scope, unit)
        now = _monotonic_us()
        if show is None or show.get("LoadState") != "loaded":
            result_units[key] = {"status": "missing", "reason": "unit not loaded"}
            continue
        if show.get("ActiveState") != "active":
            result_units[key] = {"status": "missing", "reason": "unit not active"}
            continue
        cpu_ns, started = (
            _int(show.get("CPUUsageNSec")),
            _int(show.get("ActiveEnterTimestampMonotonic")),
        )
        inv = show.get("InvocationID", "")
        rec: dict = {"status": "ok", "cpu_core_pct": None, "cpu_basis": None, "cpu_window_s": None}
        if cpu_ns is not None:
            prev = state.get(f"{scope}:{unit}") or {}
            if (
                prev.get("invocation") == inv
                and inv
                and now > prev.get("mono_us", now)
                and cpu_ns >= prev.get("cpu_ns", cpu_ns + 1)
            ):
                window_us, used_ns, basis = now - prev["mono_us"], cpu_ns - prev["cpu_ns"], "delta"
            elif started:
                window_us, used_ns, basis = now - started, cpu_ns, "since_start"
            else:
                window_us = 0
            if window_us > 0:
                rec.update(
                    cpu_core_pct=round(100 * used_ns / (window_us * 1000), 2),
                    cpu_basis=basis,
                    cpu_window_s=round(window_us / 1e6),
                )
            new_state[f"{scope}:{unit}"] = {"invocation": inv, "cpu_ns": cpu_ns, "mono_us": now}
        rec.update(
            _cgroup_memory(show.get("ControlGroup", ""))
            if show.get("ControlGroup")
            else {"rss_mb": None, "mem_current_mb": None, "mem_peak_mb": None}
        )
        rec["kills"] = _journal_count(scope, unit, end, KILL_PATTERN)
        rec["restarts"] = _journal_count(scope, unit, end, RESTART_PATTERN)
        result_units[key] = rec
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state, sort_keys=True))
        tmp.replace(state_path)
    except OSError:
        pass  # next run falls back to since_start
    folders = [f for f in re.split(r"[\s,]+", os.environ.get(SYNC_FOLDERS_ENV, "").strip()) if f]
    return {
        "status": "ok",
        "result": {
            "units_declared": len(units),
            "units": result_units,
            "disk": _disk(),
            "sync": _sync(folders),
        },
    }


def _source(reader: str) -> Path:
    env, default = SOURCES[reader]
    return Path(os.environ.get(env) or default).expanduser()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=check, capture_output=True, text=True
    )


def _run_script(scripts: Path, name: str, args: list[str]) -> dict:
    """Run one framework baseline script; return a reader record."""
    cmd = [sys.executable, "-I", str(scripts / f"baseline_{name}.py"), *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"status": "error", "reason": f"timed out after {TIMEOUT_S}s"}
    if proc.returncode != 0:
        last = (proc.stderr.strip().splitlines() or ["no stderr"])[-1]
        return {"status": "error", "reason": last[:200]}
    try:
        return {"status": "ok", "result": json.loads(proc.stdout)}
    except json.JSONDecodeError:
        return {"status": "error", "reason": "script output was not JSON"}


def _strip_content_like(value, is_content, dropped: list[str], path: str = ""):
    """Drop string-valued fields the instance validator reads as content.

    The framework's rule 7 refuses any content-like key holding a string, even a
    harmless label such as a model name. The rule is the pinned framework's own
    (``is_content``), so the collector and the validator cannot drift apart.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if is_content(str(key), item):
                dropped.append(here)
                continue
            out[key] = _strip_content_like(item, is_content, dropped, here)
        return out
    if isinstance(value, list):
        return [_strip_content_like(item, is_content, dropped, path) for item in value]
    return value


def _github(scripts: Path, services: list[dict], end: str, work: Path) -> dict:
    """Fetch merged PRs per declared service with gh, then run baseline_github."""
    pr_args: list[str] = []
    missing: list[str] = []
    for svc in services:
        name, repo = svc.get("name"), svc.get("repo")
        if not name or not repo:
            continue
        out = work / f"{name}.json"
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "-R",
                    repo,
                    "--state",
                    "merged",
                    "--limit",
                    str(GH_LIMIT),
                    "--json",
                    "number,title,mergedAt",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            missing.append(name)
            continue
        if proc.returncode != 0:
            missing.append(name)
            continue
        out.write_text(proc.stdout)
        pr_args += ["--pr", f"{name}={out}"]
    if not pr_args:
        return {
            "status": "missing",
            "reason": "no service PR list could be fetched",
            "services_missing": missing,
        }
    rec = _run_script(scripts, "github", ["--end", end, "--days", "1", *pr_args])
    if missing:
        rec["services_missing"] = missing
    return rec


def collect(instance: Path, node: str, end: str, share_model: str | None) -> dict:
    """Build the evidence record for one node and one day."""
    scripts = instance / "framework" / "scripts"
    sys.path.insert(0, str(scripts))
    from cluster_config import load_cluster_yaml  # pinned framework, trusted
    from validate_instance import _content_key  # the validator's own rule 7 test

    cluster = load_cluster_yaml((instance / "cluster.yaml").read_text())
    nodes = {n["name"]: n for n in cluster.get("nodes", [])}
    if node not in nodes:
        raise SystemExit(f"node {node!r} is not declared in cluster.yaml; refusing")
    readers: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="skrsi-collect-") as tmp:
        for reader in nodes[node].get("readers", []):
            if reader == "github":
                readers[reader] = _github(scripts, cluster.get("services", []), end, Path(tmp))
                continue
            if reader == "resources":
                readers[reader] = _resources(end)
                continue
            if reader not in SOURCES:
                readers[reader] = {
                    "status": "missing",
                    "reason": "reader not supported by this collector",
                }
                continue
            src = _source(reader)
            if not src.exists():
                readers[reader] = {
                    "status": "missing",
                    "reason": f"source not found ({SOURCES[reader][0]})",
                }
                continue
            args = {
                "cron": ["--ledger", str(src), "--days", "1", "--host", node],
                "cardstore": ["--events", str(src)],
                "gateway": ["--db", str(src), "--days", "1"],
                "sessions": ["--projects", str(src), "--days", "1"]
                + (["--share-model", share_model] if share_model else []),
            }[reader]
            readers[reader] = _run_script(scripts, reader, ["--end", end, *args])
            if reader == "sessions" and share_model:
                readers[reader]["args"] = {"share_model": share_model}
    for rec in readers.values():
        if "result" in rec:
            dropped: list[str] = []
            rec["result"] = _strip_content_like(rec["result"], _content_key, dropped)
            if dropped:
                rec["dropped_fields"] = dropped
    tag = _git(
        instance / "framework", "describe", "--tags", "--always", check=False
    ).stdout.strip()
    commit = _git(instance / "framework", "rev-parse", "HEAD", check=False).stdout.strip()
    return {
        "schema_version": 1,
        "kind": "skrsi.evidence.daily",
        "node": node,
        "date": end,
        "window_days": 1,
        "measured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "framework_ref": {"tag": tag or None, "commit": commit or None},
        "collector": "skcapstone scripts/skrsi/collect_instance.py",
        "readers": readers,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    yesterday = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)).isoformat()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--instance", required=True, type=Path, help="local clone of the skrsi instance repo"
    )
    p.add_argument(
        "--node",
        default=os.environ.get("SKRSI_NODE") or socket.gethostname().split(".")[0],
        help="node short name (default: $SKRSI_NODE, else the hostname)",
    )
    p.add_argument("--date", default=yesterday, help="day to collect, UTC (default: yesterday)")
    p.add_argument(
        "--share-model",
        default=os.environ.get(SHARE_MODEL_ENV),
        help="model for the sessions share metric",
    )
    p.add_argument(
        "--push", action="store_true", help="pull first, then commit the evidence file and push"
    )
    a = p.parse_args(argv)
    dt.date.fromisoformat(a.date)
    inst = a.instance.expanduser().resolve()
    if a.push:
        _git(inst, "pull", "--ff-only", "-q")
    out = inst / "evidence" / a.node / f"{a.date}.json"
    if out.exists():
        print(f"evidence exists, append only: {out.relative_to(inst)}")
        return 0
    record = collect(inst, a.node, a.date, a.share_model)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    val = subprocess.run(
        [sys.executable, "-I", str(inst / "framework/scripts/validate_instance.py"), str(inst)],
        capture_output=True,
        text=True,
        cwd=inst,
    )
    if val.returncode != 0:
        out.unlink()
        print(val.stdout + val.stderr, file=sys.stderr)
        return 1
    summary = {k: v["status"] for k, v in record["readers"].items()}
    print(f"wrote {out.relative_to(inst)} readers={summary}")
    if a.push:
        rel = str(out.relative_to(inst))
        _git(inst, "add", "--", rel)
        _git(
            inst,
            "-c",
            "user.name=Lumina",
            "-c",
            "user.email=lumina@skworld.io",
            "commit",
            "-q",
            "-m",
            f"evidence({a.node}): {a.date} daily collection",
            "--",
            rel,
        )
        _git(inst, "push", "-q", "origin", "HEAD:main")
        print("pushed", _git(inst, "rev-parse", "--short", "HEAD").stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
