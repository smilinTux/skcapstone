#!/usr/bin/env python3
"""Merged versus running: is what we merged actually what the fleet runs?

The estate's most expensive unanswered question, measured twice:

- 2026-09-18: the lane-model-routing fix was merged while all five chi hosts
  kept running the pre-fix ``~/.local/bin/skfleet-rotate.py`` (md5 8c400694).
  During that window the gateway served 467 requests from a 5-slot local
  fallback while codex (32 slots) served 6 and zai (10 slots) served 1. Found
  only because a separate audit happened to look.
- skcoord 0.1.57: merged, released and PyPI-verified at 07:00, not installed
  until 23:45. Sixteen hours of "fixed" that was not running anywhere. Version
  strings lied throughout: ``pip`` reported skcoord 0.1.56 while the module
  reported 0.1.0 on the same host.

The standing rule (PROPOSAL-SEATS-WITHOUT-JARVIS, section 3.2): a release is
not done when it is published, it is done when installed behaviour is verified
on every host and the versions match across all of them. A split-version fleet
is worse than an old one, because hosts then disagree about semantics.

So this check compares MERGED state (a git ref, default origin/main) against
RUNNING state on every fleet host, and reports:

- per host, per artifact: OK / DRIFT / UNKNOWN, by CONTENT DIGEST, never a
  version string. Version strings are printed as context only, and a host
  whose pip-reported version disagrees with its ``__init__``-declared version
  gets a WARN line (the exact skcoord lie).
- SPLIT FLEET as a distinct, worse finding than uniformly-behind, naming
  which hosts hold which content.
- an unreachable or unmeasurable host as UNKNOWN, never OK. "Could not
  measure" and "measured and matching" exit differently (2 versus 0).

What it measures on each host, over read-only ssh:

1. The EFFECTIVE ExecStart of the dispatcher unit, from
   ``systemctl --user show -p ExecStart``. Not the unit file: measured live,
   the unit file on every chi host says ``/usr/bin/python3`` while the
   drop-in ``seat-runtime-python.conf`` overrides the interpreter to
   ``~/.skenv/bin/python3``. Only systemd's own merged view names the real
   interpreter and the real script path.
2. The dispatcher script that ExecStart names, hashed (sha256 of the body,
   shebang line excluded: pip rewrites shebangs on install, see
   ``skcapstone.fleet.rollout_drift._sha256_script_body``).
3. The skcapstone package THE UNIT'S OWN INTERPRETER resolves: located with
   ``importlib.util.find_spec`` (which does not execute package code), then
   every ``*.py`` under it hashed and compared per file against the merged
   ref. This is behaviour-adjacent content, not a label: it catches the
   editable checkout sitting three commits behind main while every version
   string on the host still looks current.

REPORT ONLY. This never deploys, restarts, installs, or repairs anything.
Repair belongs to whichever seat owns Operations. The one optional write is
``--gtd-capture``, which upserts the finding into the unified GTD store
through the same ``skos.gtd_ingest`` port ``skcapstone.human_wait`` already
uses, deduped on a stable (source, source_ref) so a timer re-run patches one
item instead of spamming.

Output follows scripts/fleet/skfleet_readiness.py: prefixed lines a human
reads top to bottom, a final one-word-ish verdict, and an optional JSON
verdict artifact written atomically to --verdict-path.

Recommended schedule: the timer units under scripts/fleet/systemd/
(skfleet-merged-vs-running.{service,timer} by name), hand-installed on ONE
auditing host that holds ssh access to the fleet, the same hand-installed
pattern as the dispatcher unit itself. The service's OnFailure wires the
estate's existing skcapstone-alert@ hook so a drifted or unmeasurable fleet
alerts in real time, per OBSERVABILITY_AND_SCHEDULING_STANDARD.

Python 3.12, standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
DEFAULT_UNIT = "skfleet-rotate.service"
DEFAULT_REF = "origin/main"
DEFAULT_PACKAGE = "skcapstone"
PACKAGE_RELATIVE_DIR = Path("src") / "skcapstone"
DISPATCHER_RELATIVE_PATH = Path("scripts") / "fleet" / "skfleet-rotate.py"

#: How many differing module files to name per drifted host before eliding.
MAX_NAMED_FILES = 10

_ARGV_RE = re.compile(r"argv\[\]=(.*?)(?:\s*;\s*ignore_errors=|\s*})", re.DOTALL)

GTD_SOURCE = "fleet-drift"


# ---------------------------------------------------------------------------
# Pure parsing and classification (unit-tested without ssh or git).
# ---------------------------------------------------------------------------


def parse_exec_start_argv(value: str) -> list[str] | None:
    """The argv of a ``systemctl show -p ExecStart --value`` line.

    That value is systemd's merged view: the unit file PLUS every drop-in
    that overrides ExecStart. Measured live on all five chi hosts: the unit
    file names ``/usr/bin/python3`` but the effective argv names
    ``~/.skenv/bin/python3`` via seat-runtime-python.conf. Parsing the unit
    file instead of this value therefore resolves the wrong interpreter.

    Returns None when the value carries no parseable argv (unit unknown,
    empty ExecStart), which callers must treat as "could not measure", never
    as an empty-but-fine command.
    """
    if not value or "{" not in value:
        return None
    match = _ARGV_RE.search(value)
    if not match:
        return None
    argv = match.group(1).split()
    return argv or None


def dispatcher_from_argv(argv: list[str]) -> tuple[str | None, str | None]:
    """(interpreter, script path) from an effective ExecStart argv.

    A unit is entitled to its own virtualenv and declares it in ExecStart,
    so the interpreter is whatever argv[0] is when it looks like one; a unit
    that executes the script directly (shebang) has no separate interpreter.
    """
    if not argv:
        return None, None
    first = argv[0]
    if "python" in Path(first).name:
        script = next((a for a in argv[1:] if a.endswith(".py")), None)
        return first, script
    if first.endswith(".py"):
        return None, first
    return None, None


@dataclass(frozen=True)
class HostObservation:
    """One host's measured content digest for one artifact.

    ``digest`` None means the artifact could not be measured on that host,
    and ``error`` says why. That is a different fact from a digest that
    differs, and the two are never collapsed.
    """

    host: str
    digest: str | None = None
    error: str | None = None
    detail: list[str] = field(default_factory=list)


@dataclass
class ArtifactVerdict:
    """The fleet-level classification of one artifact.

    ``state`` is one of:
      in_sync           every measured host matches the merged digest
      uniformly_behind  measured hosts agree with EACH OTHER but not with
                        merged: the fleet is old, but coherent
      split_fleet       measured hosts disagree with EACH OTHER: worse than
                        old, because hosts now disagree about semantics
      unmeasured        no host could be measured at all

    ``unknown`` hosts are carried separately and never influence ``state``:
    an unreachable host is not a passing host, and it is not evidence of a
    split either. It gates the overall exit (see ``exit_code_for``).
    """

    artifact: str
    expected_digest: str
    state: str
    host_status: dict[str, str]
    groups: dict[str, list[str]]
    unknown: dict[str, str]
    observations: list[HostObservation]


def classify_artifact(
    artifact: str, expected_digest: str, observations: list[HostObservation]
) -> ArtifactVerdict:
    """Classify one artifact across the fleet. Content digests only."""
    host_status: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    unknown: dict[str, str] = {}

    for obs in observations:
        if obs.digest is None:
            host_status[obs.host] = "unknown"
            unknown[obs.host] = obs.error or "unmeasurable, no reason recorded"
            continue
        host_status[obs.host] = "ok" if obs.digest == expected_digest else "drift"
        groups.setdefault(obs.digest, []).append(obs.host)

    if not groups:
        state = "unmeasured"
    elif len(groups) > 1:
        state = "split_fleet"
    elif expected_digest in groups:
        state = "in_sync"
    else:
        state = "uniformly_behind"

    return ArtifactVerdict(
        artifact=artifact,
        expected_digest=expected_digest,
        state=state,
        host_status=host_status,
        groups=groups,
        unknown=unknown,
        observations=list(observations),
    )


def exit_code_for(verdicts: list[ArtifactVerdict]) -> int:
    """0 all measured and matching; 1 any drift; 2 no drift but UNKNOWN.

    The ranking is deliberate: drift outranks uncertainty (a measured
    problem beats a measurement gap), and uncertainty outranks green,
    because "could not measure" must never exit like "measured and fine".
    An artifact nobody could measure at all is uncertainty too.
    """
    if any(v.state in ("split_fleet", "uniformly_behind") for v in verdicts):
        return 1
    if any(v.unknown or v.state == "unmeasured" for v in verdicts):
        return 2
    return 0


def _short(digest: str) -> str:
    return digest[:12]


def render_artifact_lines(verdict: ArtifactVerdict) -> list[str]:
    """Readiness-gate style lines: a prefixed judgement per host, then one
    fleet-level line for the artifact."""
    lines: list[str] = []
    for obs in verdict.observations:
        status = verdict.host_status.get(obs.host, "unknown")
        if status == "ok":
            lines.append(
                "OK %s host %s: content matches merged (sha256 %s)"
                % (verdict.artifact, obs.host, _short(verdict.expected_digest))
            )
        elif status == "drift":
            lines.append(
                "DRIFT %s host %s: running sha256 %s, merged is %s"
                % (
                    verdict.artifact,
                    obs.host,
                    _short(obs.digest or ""),
                    _short(verdict.expected_digest),
                )
            )
            lines.extend("    " + extra for extra in obs.detail)
        else:
            lines.append(
                "UNKNOWN %s host %s: could not measure (%s); NOT counted as OK"
                % (verdict.artifact, obs.host, verdict.unknown.get(obs.host, "no reason"))
            )

    if verdict.state == "split_fleet":
        parts = ", ".join(
            "[%s]=%s" % (",".join(hosts), _short(digest))
            for digest, hosts in sorted(verdict.groups.items(), key=lambda kv: kv[1])
        )
        lines.append(
            "SPLIT %s: hosts disagree with EACH OTHER, worse than uniformly old "
            "(hosts now disagree about semantics): %s; merged is %s"
            % (verdict.artifact, parts, _short(verdict.expected_digest))
        )
    elif verdict.state == "uniformly_behind":
        digest, hosts = next(iter(verdict.groups.items()))
        lines.append(
            "BEHIND %s: all measured hosts (%s) uniformly run %s while merged is %s"
            % (
                verdict.artifact,
                ",".join(hosts),
                _short(digest),
                _short(verdict.expected_digest),
            )
        )
    elif verdict.state == "unmeasured":
        lines.append(
            "UNKNOWN %s: no host could be measured; this is not a passing state" % verdict.artifact
        )
    else:
        lines.append(
            "OK %s: fleet in sync with merged (%s)"
            % (verdict.artifact, _short(verdict.expected_digest))
        )
    return lines


def diff_package_files(expected: dict[str, str], found: dict[str, str]) -> dict[str, list[str]]:
    """Which module files differ, are missing, or are extra on a host."""
    changed = sorted(rel for rel in expected.keys() & found.keys() if expected[rel] != found[rel])
    missing = sorted(expected.keys() - found.keys())
    extra = sorted(found.keys() - expected.keys())
    return {"changed": changed, "missing": missing, "extra": extra}


def version_lie_lines(host: str, pip_version: str | None, init_version: str | None) -> list[str]:
    """A WARN when two version claims on ONE host disagree with each other.

    This is the skcoord incident verbatim: pip reported 0.1.56 while the
    module reported 0.1.0 on the same host. Version strings never decide
    the verdict here, but a host that cannot even agree with itself about
    what version it runs deserves a line of its own.
    """
    if pip_version and init_version and pip_version != init_version:
        return [
            "WARN version strings lie on %s: pip metadata says %s but __init__ "
            "declares %s; the content digests above are the authority"
            % (host, pip_version, init_version)
        ]
    return []


def _named_file_detail(diff: dict[str, list[str]]) -> list[str]:
    detail: list[str] = []
    for kind in ("changed", "missing", "extra"):
        names = diff[kind]
        if not names:
            continue
        shown = names[:MAX_NAMED_FILES]
        suffix = (
            "" if len(names) <= MAX_NAMED_FILES else " (+%d more)" % (len(names) - MAX_NAMED_FILES)
        )
        detail.append("%s: %s%s" % (kind, ", ".join(shown), suffix))
    return detail


# ---------------------------------------------------------------------------
# Expected (merged) state, from the git ref. Content digests, never a tag.
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _body_digest(data: bytes) -> str:
    """sha256 with a leading shebang line excluded: pip rewrites shebangs on
    every install, which is universal and never a fact about a host being
    wrong (same reasoning as rollout_drift._sha256_script_body)."""
    if data.startswith(b"#!"):
        _, _, data = data.partition(b"\n")
    return _sha256_bytes(data)


def _package_digest(files: dict[str, str]) -> str:
    """One digest over the whole package: sha256 of the sorted
    "relpath sha" lines, so identical trees agree regardless of walk order."""
    body = "\n".join("%s %s" % (rel, sha) for rel, sha in sorted(files.items()))
    return _sha256_bytes(body.encode())


def merged_state(repo_root: Path, ref: str, *, fetch: bool = False) -> tuple[dict, list[str]]:
    """The merged content this fleet is supposed to run, from ``ref``.

    With ``fetch``, refreshes the ref's remote first (a timer run comparing
    against a stale local ``origin/main`` would silently degrade into
    stale-versus-running, which certifies nothing). A fetch failure is a
    warning, not fatal: the comparison still runs against the last known
    merged state, and the warning line says so.

    Returns ``(state, warnings)``. Raises RuntimeError when git cannot
    answer at all, because a check that cannot name its expected state has
    nothing to compare against.
    """
    warnings: list[str] = []

    def _git(*args: str) -> bytes:
        proc = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError(
                "git %s failed: %s"
                % (" ".join(args), proc.stderr.decode(errors="replace").strip())
            )
        return proc.stdout

    if fetch and "/" in ref:
        remote, _, branch = ref.partition("/")
        try:
            _git("fetch", "--quiet", remote, branch)
        except RuntimeError as exc:
            warnings.append(
                "WARN merged ref may be stale: %s; comparing against the last "
                "locally known %s" % (exc, ref)
            )

    dispatcher_blob = _git("show", "%s:%s" % (ref, DISPATCHER_RELATIVE_PATH.as_posix()))
    ref_sha = _git("rev-parse", "--short=8", ref).decode().strip()

    files: dict[str, str] = {}
    listing = _git(
        "ls-tree",
        "-r",
        "-z",
        "--format=%(objectname) %(path)",
        ref,
        PACKAGE_RELATIVE_DIR.as_posix(),
    )
    prefix = PACKAGE_RELATIVE_DIR.as_posix() + "/"
    for entry in listing.decode().split("\0"):
        if not entry:
            continue
        objectname, _, path = entry.partition(" ")
        if not path.endswith(".py"):
            continue
        rel = path[len(prefix) :] if path.startswith(prefix) else path
        files[rel] = _sha256_bytes(_git("cat-file", "blob", objectname))

    return {
        "ref": ref,
        "ref_sha": ref_sha,
        "dispatcher_body_sha256": _body_digest(dispatcher_blob),
        "package_files": files,
        "package_digest": _package_digest(files),
    }, warnings


# ---------------------------------------------------------------------------
# Running state, per host, over read-only ssh.
# ---------------------------------------------------------------------------

#: Runs on the remote host UNDER THE UNIT'S OWN INTERPRETER, so the package
#: it locates is exactly the one the unit would import. find_spec locates
#: without executing package code; versions are read from metadata and from
#: the __init__.py text, never by importing.
REMOTE_PAYLOAD = r"""
import hashlib, importlib.metadata, importlib.util, json, re, sys
from pathlib import Path

script_path, package = sys.argv[1], sys.argv[2]
out = {"interpreter": sys.executable}

p = Path(script_path)
try:
    data = p.read_bytes()
    body = data.split(b"\n", 1)[1] if data.startswith(b"#!") else data
    out["script"] = {
        "path": str(p),
        "sha256": hashlib.sha256(data).hexdigest(),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
except OSError as exc:
    out["script"] = {"path": str(p), "error": str(exc)}

pkg = {}
try:
    spec = importlib.util.find_spec(package)
except (ImportError, ValueError) as exc:
    spec = None
    pkg["error"] = "find_spec failed: %s" % exc
if spec is None:
    pkg.setdefault("error", "package %r is not importable under %s" % (package, sys.executable))
else:
    locations = list(spec.submodule_search_locations or [])
    origin = spec.origin
    pkg_dir = Path(origin).parent if origin else (Path(locations[0]) if locations else None)
    pkg["origin"] = str(pkg_dir) if pkg_dir else None
    pkg["namespace"] = origin is None
    if pkg_dir is None:
        pkg["error"] = "package %r resolved with no location" % package
    else:
        files = {}
        for f in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            try:
                files[f.relative_to(pkg_dir).as_posix()] = hashlib.sha256(
                    f.read_bytes()
                ).hexdigest()
            except OSError:
                pass
        pkg["files"] = files
        init = pkg_dir / "__init__.py"
        try:
            m = re.search(r"__version__\s*=\s*[\"']([^\"']+)", init.read_text())
            pkg["init_version"] = m.group(1) if m else None
        except OSError:
            pkg["init_version"] = None
try:
    pkg["pip_version"] = importlib.metadata.version(package)
except importlib.metadata.PackageNotFoundError:
    pkg["pip_version"] = None
out["package"] = pkg
print(json.dumps(out))
"""


def _ssh(host: str, command: list[str], *, timeout: int, payload: str | None = None):
    """One read-only ssh exchange. Returns (stdout, error)."""
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=%d" % timeout,
        host,
        *command,
    ]
    try:
        proc = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout * 4,
        )
    except subprocess.TimeoutExpired:
        return None, "ssh to %s timed out" % host
    except OSError as exc:
        return None, "ssh to %s failed to start: %s" % (host, exc)
    if proc.returncode != 0:
        stderr = proc.stderr.strip().splitlines()
        return None, "ssh to %s exited %d (%s)" % (
            host,
            proc.returncode,
            stderr[-1] if stderr else "no stderr",
        )
    return proc.stdout, None


def collect_host(host: str, unit: str, package: str, *, timeout: int, ssh=_ssh) -> dict:
    """Measure one host: effective ExecStart, script digest, package files.

    Every failure path returns a dict whose "error" explains why the host is
    UNKNOWN; nothing here raises, and nothing here writes to the host.
    """
    show, err = ssh(
        host,
        ["systemctl", "--user", "show", unit, "-p", "ExecStart", "--value"],
        timeout=timeout,
    )
    if err is not None:
        return {"host": host, "error": err}
    argv = parse_exec_start_argv(show or "")
    if argv is None:
        return {
            "host": host,
            "error": "could not parse an effective ExecStart for %s (raw: %r)"
            % (unit, (show or "").strip()[:120]),
        }
    interpreter, script = dispatcher_from_argv(argv)
    if script is None:
        return {
            "host": host,
            "error": "effective ExecStart for %s names no python script: %r" % (unit, argv),
        }
    runner = interpreter or "python3"

    stdout, err = ssh(
        host, [runner, "-", script, package], timeout=timeout, payload=REMOTE_PAYLOAD
    )
    if err is not None:
        return {"host": host, "error": err, "exec_argv": argv}
    try:
        report = json.loads(stdout)
    except ValueError:
        return {
            "host": host,
            "error": "remote payload under %s printed non-JSON" % runner,
            "exec_argv": argv,
        }
    report["host"] = host
    report["exec_argv"] = argv
    report["unit"] = unit
    return report


# ---------------------------------------------------------------------------
# Assembly: expected versus every host, lines, verdict artifact, GTD.
# ---------------------------------------------------------------------------


def build_verdicts(
    expected: dict, host_reports: list[dict]
) -> tuple[list[ArtifactVerdict], list[str]]:
    """Fold host reports into per-artifact verdicts plus context lines."""
    dispatcher_obs: list[HostObservation] = []
    package_obs: list[HostObservation] = []
    context: list[str] = []

    for report in host_reports:
        host = report["host"]
        if "error" in report and "script" not in report:
            dispatcher_obs.append(HostObservation(host, error=report["error"]))
            package_obs.append(HostObservation(host, error=report["error"]))
            continue

        script = report.get("script", {})
        if "error" in script:
            dispatcher_obs.append(
                HostObservation(
                    host, error="script %s: %s" % (script.get("path"), script["error"])
                )
            )
        else:
            dispatcher_obs.append(HostObservation(host, digest=script.get("body_sha256")))
            context.append(
                "INFO dispatcher host %s: unit %s runs %s via %s"
                % (
                    host,
                    report.get("unit"),
                    script.get("path"),
                    report.get("interpreter") or "(direct)",
                )
            )

        pkg = report.get("package", {})
        if "files" not in pkg:
            package_obs.append(
                HostObservation(host, error=pkg.get("error", "package not measured"))
            )
        else:
            diff = diff_package_files(expected["package_files"], pkg["files"])
            package_obs.append(
                HostObservation(
                    host,
                    digest=_package_digest(pkg["files"]),
                    detail=_named_file_detail(diff),
                )
            )
            context.append(
                "INFO library host %s: %s resolves %s (pip says %s)"
                % (
                    host,
                    report.get("interpreter") or "python3",
                    pkg.get("origin"),
                    pkg.get("pip_version"),
                )
            )
            context.extend(
                version_lie_lines(host, pkg.get("pip_version"), pkg.get("init_version"))
            )

    verdicts = [
        classify_artifact(
            "dispatcher:%s" % DISPATCHER_RELATIVE_PATH.name,
            expected["dispatcher_body_sha256"],
            dispatcher_obs,
        ),
        classify_artifact("library:skcapstone", expected["package_digest"], package_obs),
    ]
    return verdicts, context


def write_verdict(path: Path, payload: dict) -> None:
    """Atomic JSON write, the same tmp-then-replace shape the readiness gate
    uses, so a reader never observes a half-written verdict."""
    data = json.dumps(payload, sort_keys=True, indent=2).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def gtd_capture(verdicts: list[ArtifactVerdict], expected: dict, lines: list[str]) -> list[str]:
    """Upsert each non-green artifact into the unified GTD store.

    Same port and same idempotency contract as skcapstone.human_wait: the
    (source, source_ref) pair is stable per artifact, so a timer re-run
    patches the standing item ("unchanged" performs no write) instead of
    filing a new one every tick. Never raises: if skos is not importable on
    the auditing host, the finding still lands via exit code and OnFailure
    alert, and a line here says capture was unavailable.
    """
    out: list[str] = []
    try:
        from skos.gtd_ingest import GtdCapture, upsert
    except ImportError as exc:
        return ["WARN gtd capture unavailable (%s); finding lands via exit code only" % exc]

    for verdict in verdicts:
        if verdict.state == "in_sync" and not verdict.unknown:
            continue
        text = "[fleet-drift] %s is %s vs merged %s (%s)" % (
            verdict.artifact,
            verdict.state.replace("_", " "),
            expected["ref"],
            expected["ref_sha"],
        )
        try:
            item_id, action = upsert(
                GtdCapture(
                    text=text,
                    source=GTD_SOURCE,
                    source_ref="merged-vs-running:%s" % verdict.artifact,
                    context="@ops",
                    meta={
                        "state": verdict.state,
                        "ref": expected["ref"],
                        "ref_sha": expected["ref_sha"],
                        "host_status": verdict.host_status,
                        "unknown": verdict.unknown,
                    },
                )
            )
            out.append("INFO gtd: %s %s (%s)" % (action, item_id, verdict.artifact))
        except Exception as exc:  # capture must never break the report
            out.append("WARN gtd capture failed for %s: %s" % (verdict.artifact, exc))
    return out


VERDICT_WORDS = {0: "IN SYNC", 1: "DRIFT DETECTED", 2: "UNCERTAIN: not every host measured"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare MERGED state (a git ref) against RUNNING state on every "
        "fleet host. Reports only; never deploys, restarts, or repairs."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[2]),
        help="Checkout to read merged content from (default: this script's repo)",
    )
    parser.add_argument("--ref", default=DEFAULT_REF, help="Merged ref (default: %(default)s)")
    parser.add_argument(
        "--hosts",
        default=",".join(DEFAULT_HOSTS),
        help="Comma-separated fleet hosts (default: %(default)s)",
    )
    parser.add_argument(
        "--unit", default=DEFAULT_UNIT, help="Dispatcher unit (default: %(default)s)"
    )
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help=argparse.SUPPRESS)
    parser.add_argument(
        "--ssh-timeout", type=int, default=10, help="Per-connection timeout, seconds"
    )
    parser.add_argument(
        "--verdict-path",
        default=None,
        help="Optional path for the machine-readable verdict JSON (atomic write)",
    )
    parser.add_argument(
        "--gtd-capture",
        action="store_true",
        help="Upsert non-green findings into the unified GTD store (deduped on a "
        "stable source_ref). Off by default so an on-demand run writes nothing.",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="git fetch the ref's remote first, so a timer compares against a "
        "FRESH merged state instead of a stale local ref. Fetch failure warns "
        "and continues.",
    )
    args = parser.parse_args(argv)

    lines: list[str] = []
    try:
        expected, warnings = merged_state(Path(args.repo_root), args.ref, fetch=args.fetch)
    except RuntimeError as exc:
        print("FAIL merged state: %s" % exc)
        print(VERDICT_WORDS[2])
        return 2
    lines.extend(warnings)
    lines.append(
        "INFO merged: %s at %s, dispatcher %s, library digest %s (%d module files)"
        % (
            args.ref,
            expected["ref_sha"],
            _short(expected["dispatcher_body_sha256"]),
            _short(expected["package_digest"]),
            len(expected["package_files"]),
        )
    )

    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    reports = [
        collect_host(host, args.unit, args.package, timeout=args.ssh_timeout) for host in hosts
    ]

    verdicts, context = build_verdicts(expected, reports)
    lines.extend(context)
    for verdict in verdicts:
        lines.extend(render_artifact_lines(verdict))

    if args.gtd_capture:
        lines.extend(gtd_capture(verdicts, expected, lines))

    code = exit_code_for(verdicts)

    if args.verdict_path:
        payload = {
            "in_sync": code == 0,
            "exit_code": code,
            "checked_at": datetime.datetime.now(datetime.timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "ref": expected["ref"],
            "ref_sha": expected["ref_sha"],
            "lines": lines + [VERDICT_WORDS[code]],
            "artifacts": [
                {
                    "artifact": v.artifact,
                    "state": v.state,
                    "expected_digest": v.expected_digest,
                    "host_status": v.host_status,
                    "groups": v.groups,
                    "unknown": v.unknown,
                }
                for v in verdicts
            ],
        }
        try:
            write_verdict(Path(args.verdict_path), payload)
        except OSError as exc:
            lines.append("FAIL verdict: could not write %s (%s)" % (args.verdict_path, exc))
            code = max(code, 2)

    print("\n".join(lines))
    print(VERDICT_WORDS[code])
    return code


if __name__ == "__main__":
    sys.exit(main())
