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

BOUNDARY OF AUTHORITY
    Observation only. It never approves, merges or deploys anything. Pushing
    needs a branch rule that lets the pushing account write ``evidence/**``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import tempfile
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


def _source(reader: str) -> Path:
    env, default = SOURCES[reader]
    return Path(os.environ.get(env) or default).expanduser()


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


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
                ["gh", "pr", "list", "-R", repo, "--state", "merged", "--limit", str(GH_LIMIT),
                 "--json", "number,title,mergedAt"],
                capture_output=True, text=True, timeout=120,
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
        return {"status": "missing", "reason": "no service PR list could be fetched", "services_missing": missing}
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
            if reader not in SOURCES:
                readers[reader] = {"status": "missing", "reason": "reader not supported by this collector"}
                continue
            src = _source(reader)
            if not src.exists():
                readers[reader] = {"status": "missing", "reason": f"source not found ({SOURCES[reader][0]})"}
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
    tag = _git(instance / "framework", "describe", "--tags", "--always", check=False).stdout.strip()
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
    p.add_argument("--instance", required=True, type=Path, help="local clone of the skrsi instance repo")
    p.add_argument("--node", default=socket.gethostname().split(".")[0], help="node short name (default: hostname)")
    p.add_argument("--date", default=yesterday, help="day to collect, UTC (default: yesterday)")
    p.add_argument("--share-model", default=os.environ.get(SHARE_MODEL_ENV), help="model for the sessions share metric")
    p.add_argument("--push", action="store_true", help="pull first, then commit the evidence file and push")
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
    val = subprocess.run([sys.executable, "-I", str(inst / "framework/scripts/validate_instance.py"), str(inst)],
                         capture_output=True, text=True, cwd=inst)
    if val.returncode != 0:
        out.unlink()
        print(val.stdout + val.stderr, file=sys.stderr)
        return 1
    summary = {k: v["status"] for k, v in record["readers"].items()}
    print(f"wrote {out.relative_to(inst)} readers={summary}")
    if a.push:
        rel = str(out.relative_to(inst))
        _git(inst, "add", "--", rel)
        _git(inst, "-c", "user.name=Lumina", "-c", "user.email=lumina@skworld.io", "commit", "-q",
             "-m", f"evidence({a.node}): {a.date} daily collection", "--", rel)
        _git(inst, "push", "-q", "origin", "HEAD:main")
        print("pushed", _git(inst, "rev-parse", "--short", "HEAD").stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
