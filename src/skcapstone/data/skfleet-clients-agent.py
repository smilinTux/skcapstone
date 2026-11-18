#!/usr/bin/env python3
"""skfleet-clients host agent: install pinned clients and merge managed configs.

Runs ON the target host. `skcapstone fleet clients` on chiap08 copies it to
``~/.local/lib/skfleet/clients-agent.py`` and runs

    ssh HOST python3 ~/.local/lib/skfleet/clients-agent.py ACTION < payload.json

reading one JSON object back on stdout. Stdlib
only, so it does not depend on the host's skcapstone version. Secrets never
leave the host: ``$secret:<name>`` placeholders are filled from the host's own
live configs, and reports mask every preserved key.

Actions: inspect, apply, gate, smoke, rollback, prune.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".local/state/skfleet-clients"
UNDO = STATE_DIR / "undo.json"
MANAGED_MARK = HOME / ".config/skfleet/clients-managed"
SECRET_PREFIX = "$secret:"
SHIM_TAG = "# skfleet-clients shim"
BACKUP_KEEP = 10
SECRET_KEYS = re.compile(r"key|token|secret|password", re.I)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def now_tag() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def run(cmd: list[str], *, timeout: float = 600, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)


# ------------------------------------------------------------------ installs


def version_dir(root: str, client: dict) -> Path:
    return expand(root) / client["name"] / client["version"]


def install(root: str, client: dict) -> str:
    """Install one pinned version into its own dir. Idempotent."""
    vdir = version_dir(root, client)
    entry = vdir / client["entry"]
    if entry.exists() and (vdir / ".skfleet-complete").exists():
        return "present"
    vdir.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{client['version']}.", dir=vdir.parent))
    try:
        if client["source"] == "npm":
            res = run(["npm", "install", "--prefix", str(tmp), "--no-fund", "--no-audit",
                       "--loglevel=error", f"{client['package']}@{client['version']}"], timeout=900)
            if res.returncode != 0:
                raise RuntimeError(f"npm install failed: {res.stderr.strip()[-400:]}")
        elif client["source"] == "release":
            blob = tmp / "download"
            with urllib.request.urlopen(client["url"], timeout=120) as resp, open(blob, "wb") as fh:
                shutil.copyfileobj(resp, fh)
            digest = hashlib.sha256(blob.read_bytes()).hexdigest()
            if digest != client["sha256"]:
                raise RuntimeError(f"sha256 mismatch for {client['url']}: {digest}")
            with tarfile.open(blob) as tar:
                tar.extractall(tmp, filter="data")
            blob.unlink()
        else:
            raise RuntimeError(f"unknown source {client['source']}")
        if not (tmp / client["entry"]).exists():
            raise RuntimeError(f"entry {client['entry']} missing after install")
        (tmp / ".skfleet-complete").write_text(client["version"] + "\n")
        if vdir.exists():
            shutil.rmtree(vdir)
        tmp.rename(vdir)
        return "installed"
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def shim_text(root: str, client: dict) -> str:
    target = version_dir(root, client) / client["entry"]
    lines = ["#!/bin/sh", f"{SHIM_TAG} {client['name']} {client['version']}"]
    for key, val in sorted(client.get("env", {}).items()):
        lines.append(f"export {key}='{val}'")
    lines.append(f'exec "{target}" "$@"')
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def save_displaced(path: Path, root: str, undo: dict) -> None:
    """Keep whatever we replace, so rollback can put it back."""
    if not (path.exists() or path.is_symlink()):
        undo["shims"][str(path)] = None
        return
    if path.is_symlink():
        undo["shims"][str(path)] = {"symlink": os.readlink(path)}
        return
    keep = expand(root) / "_displaced" / f"{path.name}-{now_tag()}"
    keep.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, keep)
    undo["shims"][str(path)] = {"file": str(keep)}


# ------------------------------------------------------------------ configs


def load_doc(path: Path, fmt: str):
    if not path.exists():
        return None
    raw = path.read_text()
    return json.loads(raw) if fmt == "json" else raw


def get_path(doc, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def gateway_secrets() -> dict:
    """Name -> value for $secret placeholders, read from this host's own configs."""
    found = {}
    pi = HOME / ".pi/agent/models.json"
    oc = HOME / ".config/opencode/opencode.json"
    try:
        for name, prov in (json.loads(pi.read_text()).get("providers") or {}).items():
            if isinstance(prov, dict) and prov.get("apiKey") and ":18790" in str(prov.get("baseUrl")):
                found.setdefault("skgateway", prov["apiKey"])
    except (OSError, ValueError):
        pass
    try:
        prov = json.loads(oc.read_text()).get("provider", {}).get("skgateway", {})
        key = (prov.get("options") or {}).get("apiKey")
        if key:
            found.setdefault("skgateway", key)
    except (OSError, ValueError, AttributeError):
        pass
    return found


def fill(value, live, preserve: set, secrets: dict, missing: list):
    """Template value merged with the live one: preserved keys come from live,
    $secret placeholders from the host, {home} becomes $HOME."""
    if isinstance(value, dict):
        out = {}
        live_d = live if isinstance(live, dict) else {}
        for key, sub in value.items():
            out[key] = fill(sub, live_d.get(key), preserve, secrets, missing)
        for key in preserve:
            if key in live_d and key not in value:
                out[key] = copy.deepcopy(live_d[key])
            elif key in live_d and isinstance(value.get(key), str) and value[key].startswith(SECRET_PREFIX):
                out[key] = live_d[key]
        return out
    if isinstance(value, list):
        return [fill(v, None, preserve, secrets, missing) for v in value]
    if isinstance(value, str):
        if value.startswith(SECRET_PREFIX):
            if isinstance(live, str) and live and not live.startswith(SECRET_PREFIX):
                return live
            name = value[len(SECRET_PREFIX):]
            if name in secrets:
                return secrets[name]
            missing.append(name)
            return value
        return value.replace("{home}", str(HOME))
    return value


def set_path(doc: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = cur[part] = {}
        cur = nxt
    cur[parts[-1]] = value


def toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def merge_toml(text: str, managed: dict) -> str:
    """Set top-level scalar keys without disturbing the rest of the file."""
    lines = text.splitlines()
    first_table = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
    for key, value in managed.items():
        rx = re.compile(rf"^\s*{re.escape(key)}\s*=")
        line = f"{key} = {toml_value(value)}"
        for i in range(first_table):
            if rx.match(lines[i]):
                lines[i] = line
                break
        else:
            lines.insert(0, line)
            first_table += 1
    out = "\n".join(lines) + "\n"
    tomllib.loads(out)  # refuse to write a file codex cannot parse
    return out


def render_file(spec: dict, secrets: dict):
    """Return (new_text, old_text, missing_secrets, skipped_reason)."""
    path = expand(spec["file"])
    fmt = spec["format"]
    old = path.read_text() if path.exists() else None
    if spec.get("only_existing") and old is None:
        return None, None, [], "file absent"
    missing: list = []
    preserve = set(spec.get("preserve") or [])
    if fmt == "json":
        doc = json.loads(old) if old else {}
        for dotted, value in spec["set"].items():
            if spec.get("only_existing") and get_path(doc, dotted.rsplit(".", 1)[0]) is None:
                continue
            set_path(doc, dotted, fill(value, get_path(doc, dotted), preserve, secrets, missing))
        new = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    else:
        new = merge_toml(old or "", spec["set"])
    return new, old, missing, None


def backup(path: Path) -> str:
    bdir = path.parent / "backups" / "skfleet-clients"
    bdir.mkdir(parents=True, exist_ok=True)
    dest = bdir / f"{path.name}.{now_tag()}"
    shutil.copy2(path, dest)
    for old in sorted(bdir.glob(f"{path.name}.*"))[:-BACKUP_KEEP]:
        old.unlink()
    return str(dest)


def mask(obj):
    if isinstance(obj, dict):
        return {k: ("***" if SECRET_KEYS.search(k) and isinstance(v, str) else mask(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


def config_summary(spec: dict, new: str | None, old: str | None) -> dict:
    """Which managed paths change, without values (secret-safe)."""
    if new is None:
        return {"file": spec["file"], "skipped": True}
    if old == new:
        return {"file": spec["file"], "changed": []}
    if spec["format"] == "json":
        a = json.loads(old) if old else {}
        b = json.loads(new)
        changed = [p for p in spec["set"] if json.dumps(mask(get_path(a, p)), sort_keys=True)
                   != json.dumps(mask(get_path(b, p)), sort_keys=True)]
    else:
        changed = list(spec["set"])
    return {"file": spec["file"], "changed": changed, "created": old is None}


# ------------------------------------------------------------------ actions


def resolve(name: str) -> list[str]:
    res = run(["bash", "-lc", f"type -ap {name}"], timeout=30)
    return [l for l in res.stdout.splitlines() if l.strip()]


def client_version(path: Path) -> str:
    try:
        res = run([str(path), "--version"], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"ERR {type(exc).__name__}"
    m = re.search(r"\d+\.\d+\.\d+", res.stdout + res.stderr)
    return m.group(0) if m else "ERR " + (res.stdout + res.stderr).strip()[:80]


def inspect(p: dict) -> dict:
    bin_dir = expand(p["bin_dir"])
    out = {"clients": {}, "files": []}
    for c in p["clients"]:
        shim = bin_dir / c["name"]
        text = shim.read_text(errors="replace") if shim.is_file() else ""
        found = resolve(c["name"])
        out["clients"][c["name"]] = {
            "managed": SHIM_TAG in text,
            "shim_ok": text == shim_text(p["install_root"], c),
            "first": found[0] if found else None,
            "others": found[1:],
            "version": client_version(Path(found[0])) if found else None,
        }
    secrets = gateway_secrets()
    for spec in p["files"]:
        new, old, missing, skipped = render_file(spec, secrets)
        row = config_summary(spec, new, old)
        row["missing_secrets"] = missing
        out["files"].append(row)
    return out


def apply(p: dict) -> dict:
    root, bin_dir = p["install_root"], expand(p["bin_dir"])
    undo = {"at": now_tag(), "shims": {}, "files": {}}
    report = {"installs": {}, "files": [], "errors": []}
    for c in p["clients"]:
        try:
            report["installs"][c["name"]] = install(root, c)
        except Exception as exc:  # noqa: BLE001 - reported, gate fails
            report["errors"].append(f"install {c['name']}: {exc}")
    if report["errors"]:
        return report
    for c in p["clients"]:
        shim = bin_dir / c["name"]
        text = shim_text(root, c)
        if shim.is_file() and not shim.is_symlink() and shim.read_text(errors="replace") == text:
            continue
        save_displaced(shim, root, undo)
        if shim.is_symlink():
            shim.unlink()
        write_atomic(shim, text, 0o755)
    secrets = gateway_secrets()
    for spec in p["files"]:
        path = expand(spec["file"])
        try:
            new, old, missing, skipped = render_file(spec, secrets)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append(f"render {spec['file']}: {type(exc).__name__}: {exc}")
            continue
        row = config_summary(spec, new, old)
        if missing:
            # No key on this host for this config: leave the file alone, report it.
            row["skipped"] = f"missing secrets {sorted(set(missing))}"
            report["files"].append(row)
            continue
        if new is not None and new != old:
            undo["files"][str(path)] = backup(path) if old is not None else None
            mode = path.stat().st_mode & 0o777 if old is not None else 0o600
            write_atomic(path, new, mode)
        report["files"].append(row)
    MANAGED_MARK.parent.mkdir(parents=True, exist_ok=True)
    MANAGED_MARK.write_text(json.dumps({"at": undo["at"], "state": p.get("state_hash")}) + "\n")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if undo["shims"] or undo["files"]:
        UNDO.write_text(json.dumps(undo, indent=2) + "\n")
    report["undo"] = bool(undo["shims"] or undo["files"])
    return report


def rollback(p: dict) -> dict:
    if not UNDO.exists():
        return {"rolled_back": False, "reason": "no undo record"}
    undo = json.loads(UNDO.read_text())
    for shim, prev in undo["shims"].items():
        path = Path(shim)
        if path.exists() or path.is_symlink():
            path.unlink()
        if prev and "symlink" in prev:
            os.symlink(prev["symlink"], path)
        elif prev and "file" in prev:
            shutil.copy2(prev["file"], path)
    for target, saved in undo["files"].items():
        path = Path(target)
        if saved:
            shutil.copy2(saved, path)
        elif path.exists():
            path.unlink()
    UNDO.rename(UNDO.with_suffix(f".used-{now_tag()}"))
    return {"rolled_back": True, "at": undo["at"]}


def gate(p: dict) -> dict:
    """Host gate: pinned versions answer via the shim first on PATH, configs hold."""
    failures = []
    view = inspect(p)
    bin_dir = expand(p["bin_dir"])
    for c in p["clients"]:
        row = view["clients"][c["name"]]
        if not row["shim_ok"]:
            failures.append(f"{c['name']}: shim missing or stale")
        if row["first"] != str(bin_dir / c["name"]):
            failures.append(f"{c['name']}: PATH resolves {row['first']} first")
        if row["version"] != c["version"]:
            failures.append(f"{c['name']}: version {row['version']} != {c['version']}")
    for row in view["files"]:
        if row.get("skipped") is True:
            continue
        if row.get("missing_secrets"):
            continue  # reported as drift, not a gate failure
        if row.get("changed"):
            failures.append(f"{row['file']}: managed keys not applied: {row['changed']}")
    expect = p.get("pi_models") or []
    if expect and any(c["name"] == "pi" for c in p["clients"]):
        res = run([str(bin_dir / "pi"), "--list-models"], timeout=120)
        listed = {tuple(l.split()[:2]) for l in res.stdout.splitlines() if l.strip()}
        lost = [f"{prov}/{mid}" for prov, mid in expect if (prov, mid) not in listed]
        if res.returncode != 0 or lost:
            failures.append(f"pi --list-models misses {lost[:5]} (rc {res.returncode})")
    return {"ok": not failures, "failures": failures, "view": view}


def smoke(p: dict) -> dict:
    """One tiny real prompt per client. With candidate=True, run the version
    dir binaries directly (upstream check before the pin moves)."""
    results = {}
    for c in p["clients"]:
        if not c.get("smoke"):
            continue
        if p.get("candidate"):
            install(p["install_root"], c)
            exe = str(version_dir(p["install_root"], c) / c["entry"])
            env = {**os.environ, **c.get("env", {})}
        else:
            exe, env = str(expand(p["bin_dir"]) / c["name"]), None
        try:
            res = run([exe, *c["smoke"]], timeout=240, env=env)
            text = res.stdout + res.stderr
            ok = res.returncode == 0 and c["smoke_expect"] in text
            results[c["name"]] = {"ok": ok, "rc": res.returncode, "tail": "" if ok else text.strip()[-300:]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            results[c["name"]] = {"ok": False, "rc": None, "tail": type(exc).__name__}
    return {"ok": all(r["ok"] for r in results.values()), "results": results}


def prune(p: dict) -> dict:
    """Keep the pinned version and the newest other one per client."""
    removed = []
    for c in p["clients"]:
        cdir = expand(p["install_root"]) / c["name"]
        if not cdir.is_dir():
            continue
        others = sorted((d for d in cdir.iterdir() if d.is_dir() and d.name != c["version"]
                         and not d.name.startswith(".")), key=lambda d: d.stat().st_mtime)
        for d in others[:-1]:
            shutil.rmtree(d)
            removed.append(str(d))
    return {"removed": removed}


ACTIONS = {"inspect": inspect, "apply": apply, "gate": gate, "smoke": smoke,
           "rollback": rollback, "prune": prune}


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in ACTIONS:
        print(json.dumps({"error": f"unknown action {action!r}"}))
        return 2
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        result = ACTIONS[action](payload)
    except Exception as exc:  # noqa: BLE001 - one JSON answer, always
        result = {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
