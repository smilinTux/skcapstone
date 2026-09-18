"""Rollout drift detection: what a host is actually running, not what its
labels claim.

See ``docs/superpowers/specs/2026-09-16-nimble-factory-design.md``, section
A.6, and ``.superpowers/sdd/2026-09-17-rollout-observability/task-3-brief.md``.
This exists because four real drift incidents were measured on this fleet in
one day, and a version check would have reported every host healthy during
all four of them:

  skmail            three different binaries across five hosts, none
                     matching the repo, invisible to any version check
                     because the file was never in pyproject's script-files
                     and so belonged to no package at all.
  dispatcher script ``~/.local/bin/skfleet-rotate.py`` is a COPY, deployed
                     separately from the package. It sat stale on hosts
                     whose package was current.
  seat units         chiap08 carried a seat unit FAILED for weeks.
  timer enablement   skfleet-rotate.timer was active but NOT enabled on all
                     three rotate hosts for at least 7 weeks. A reboot would
                     have stopped fleet dispatch everywhere and nothing would
                     have reported it.

Throughout all of it, ``pip show skcapstone`` and the module's
``__version__`` agreed on every host. So this module compares CONTENT and
STATE, never a version label: a digest of each shipped unit file, a digest
of the installed dispatcher script, the git_sha embedded in the installed
distribution (a content identifier, not a semantic version number), and
whether a unit's enabled state agrees with its active state.

Read-only and cheap: every check is a file read, a glob, or a handful of
``systemctl --user show`` calls (Task 2's own pattern, reused rather than
reinvented). Nothing here writes to a host, restarts a unit, or installs
anything.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .deployment_manifest import CANONICAL_SYSTEMD_RELATIVE_DIR, DISPATCHER_RELATIVE_PATH
from .paths import self_node_name

#: Not in the shipped systemd/ tree (see the module docstring: the dispatcher
#: script is deployed by a separate mechanism from the package), but it is
#: still a real, live systemd unit name on rotate hosts, so it can still be
#: asked about via unit_in_scope the same way any other role unit can.
DISPATCHER_UNIT_NAME = "skfleet-rotate.service"

#: Units that are real, live, and referenced by name, but are deliberately
#: never templated under src/skcapstone/data/systemd/: skfleet-rotate.service
#: (and its timer) are hand-installed per host at
#: ~/.config/systemd/user/skfleet-rotate.service, documented in
#: docs/fleet/model-lane-routing.md. That is a different install path from
#: the packaged lifecycle-seat units the shipped-unit tree protects.
#:
#: This is the single source of truth for that fact, reused rather than
#: duplicated: tests/fleet/test_no_dangling_systemd_unit_references.py
#: imports this exact set as its own allowlist. The enablement check below
#: reuses it for the reason that test exists to guard against a second
#: hand-maintained list drifting from the first: the timer-enablement
#: incident this module's docstring documents (skfleet-rotate.timer active
#: but not enabled on all three rotate hosts for at least 7 weeks) happened
#: to a unit that is exactly hand-installed and exactly not in
#: manifest["units"] -- if the enablement check below only ever walked
#: shipped units, it could never have caught the one incident it was built
#: for.
ALLOWED_UNSHIPPED_UNITS = frozenset({"skfleet-rotate.service", "skfleet-rotate.timer"})

_DIST_INFO_GIT_SHA_RE = re.compile(r"\+g([0-9a-f]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Drift:
    """One difference between what a host runs and what its manifest pins.

    ``kind`` is the operational signal a reader acts on first: "missing" and
    "changed" are different problems with different fixes (reinstall versus
    diff-and-decide), so they are never collapsed into one label.
    ``enablement_mismatch`` covers the timer-enablement incident, which no
    content digest can catch because the unit FILE is correct; only its
    wants-symlink state is wrong.
    """

    artifact: str
    kind: str  # "missing" | "changed" | "enablement_mismatch"
    expected: str
    found: str | None
    host: str


def _sha256_file(path: Path) -> str | None:
    """The content digest of ``path``, or None when it cannot be read.

    None (never an empty string or a fabricated digest) is the signal
    detect_drift uses to tell "this artifact is missing" from "this artifact
    is present with different content" -- the two are different operational
    problems and must stay distinguishable end to end.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _installed_git_sha(home: Path) -> str | None:
    """The git commit embedded in the installed skcapstone distribution.

    ``setuptools_scm`` bakes the short git hash into the version it derives
    from the git tag, and that version is part of the dist-info directory
    name itself (e.g. ``skcapstone-0.15.168.dev165+g723e6a98.dist-info``), so
    this needs only a glob under the one place SK* packages install
    (``~/.skenv``, per this estate's own convention) -- no subprocess, no
    file open.

    This is the one place a version STRING is consulted, and it is not the
    naive check this module exists to distrust: a semantic version number
    (0.15.168) reads identically across unrelated commits, which is exactly
    what let the skmail incident hide. The embedded git hash is a content
    identifier of the exact commit the installed distribution was built
    from -- the same kind of fact ``git_sha`` itself is, not a label.
    """
    for dist_info in sorted(
        home.glob(".skenv/lib/python3.*/site-packages/skcapstone-*.dist-info")
    ):
        match = _DIST_INFO_GIT_SHA_RE.search(dist_info.name)
        if match:
            return match.group(1).lower()
    return None


def _sha_matches(expected: str, found: str) -> bool:
    """Compare two git short-shas that may not share one abbreviation length.

    The manifest's ``git_sha`` is a fixed ``git rev-parse --short=8``.
    ``setuptools_scm``'s ``git describe`` abbreviates to the shortest unique
    length, which is not guaranteed to also be 8 (Task 1 measured this
    exact mismatch on one checkout: package_version's embedded hash and
    git_sha disagreed in length though both described the same repository
    state). Comparing the shorter as a prefix of the longer avoids reporting
    length alone as drift, while a genuine content mismatch still fails.
    """
    lower_expected, lower_found = expected.lower(), found.lower()
    shorter, longer = (
        (lower_expected, lower_found)
        if len(lower_expected) <= len(lower_found)
        else (lower_found, lower_expected)
    )
    if not shorter:
        return False
    return longer.startswith(shorter)


def _load_readiness_module(repo_root: Path):
    """Load ``scripts/fleet/skfleet_readiness.py`` by path, reusing an
    already-imported copy when one exists.

    Reuses Task 2's role-scoping (``unit_in_scope``) and its
    ``_systemctl_show_value`` helper rather than re-implementing "ask
    systemd for one property of one unit" a second time. Checking
    ``sys.modules`` first (registering under the plain name
    ``skfleet_readiness``, matching how ``tests/fleet/test_readiness_wiring.py``
    and ``tests/test_skfleet_readiness.py`` already import it after adding
    ``scripts/fleet`` to ``sys.path``) means a test's monkeypatched
    ``subprocess.run`` on that module is the same object detect_drift's live
    systemctl calls use, and a second call within one process does not pay
    for a second disk read. Safe to share: the module is stdlib-only and
    carries no per-caller state.
    """
    cached = sys.modules.get("skfleet_readiness")
    if cached is not None:
        return cached
    gate_path = Path(repo_root) / "scripts" / "fleet" / "skfleet_readiness.py"
    spec = importlib.util.spec_from_file_location("skfleet_readiness", gate_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load the readiness gate module from {gate_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["skfleet_readiness"] = module
    spec.loader.exec_module(module)
    return module


def detect_drift(manifest: dict[str, Any], home: Path | str, repo_root: Path | str) -> list[Drift]:
    """Compare what ``home`` actually runs against ``manifest``.

    Args:
        manifest: A dict built by ``deployment_manifest.build_manifest``
            (or an equivalent with ``git_sha`` and ``units``), naming what
            this node is supposed to run.
        home: The node's estate home. Installed artifacts are read from
            here: unit files under ``~/.config/systemd/user``, the
            dispatcher script under ``~/.local/bin``, and the installed
            package under ``~/.skenv``.
        repo_root: A checkout supplying the EXPECTED content: the shipped
            ``systemd/`` tree and ``scripts/fleet/skfleet-rotate.py``.

    Returns:
        Every drift found, each naming the artifact, whether it is missing,
        changed, or an enablement/activity mismatch, the expected and found
        values, and the host. Empty when the host genuinely matches.

    Scope: a unit or the dispatcher script is skipped entirely when Task 2's
    ``unit_in_scope`` determines it is genuinely out of scope for this host
    (chiap08 does not run the dispatcher; chiap01/02/03 do -- neither is
    drift). An UNDETERMINED scope (systemd could not say) is never treated
    as "skip": the content check still runs, on the same "unknown is never
    ready" reasoning ``unit_in_scope`` itself documents. This also
    correctly covers units that have no paired timer at all (the core
    daemons, e.g. skcapstone.service): ``unit_in_scope`` reports those
    undetermined too (their "paired timer" does not exist), and undetermined
    means checked, which is what a unit that runs on every host needs.

    The enablement check (kind ``enablement_mismatch``) is not limited to
    ``manifest["units"]``: it also covers ``ALLOWED_UNSHIPPED_UNITS``, the
    critical units that are hand-installed per host rather than shipped
    (currently just ``skfleet-rotate.service``/``.timer``). They are real,
    live, in-scope units that can go active-but-not-enabled exactly like a
    shipped one, and being unshipped is the whole reason the incident this
    check exists for went undetected for at least 7 weeks: a shipped-only
    loop cannot see a unit that is never shipped.
    """
    home = Path(home)
    repo_root = Path(repo_root)
    host = self_node_name()
    readiness = _load_readiness_module(repo_root)

    drifts: list[Drift] = []
    scope_cache: dict[str, tuple[bool | None, str | None, str]] = {}

    def scope_of(unit_name: str) -> tuple[bool | None, str | None, str]:
        if unit_name not in scope_cache:
            scope_cache[unit_name] = readiness.unit_in_scope(unit_name)
        return scope_cache[unit_name]

    # 1. Each shipped unit file: content digest, scoped by role.
    for unit_name in manifest.get("units", []):
        in_scope, _scope_error, _checked_unit = scope_of(unit_name)
        if in_scope is False:
            continue  # legitimate local state: this role does not apply here

        expected_path = repo_root / CANONICAL_SYSTEMD_RELATIVE_DIR / unit_name
        expected_digest = _sha256_file(expected_path)
        if expected_digest is None:
            continue  # manifest names a unit the repo no longer ships

        installed_path = home / ".config" / "systemd" / "user" / unit_name
        found_digest = _sha256_file(installed_path)
        artifact = f"unit:{unit_name}"
        if found_digest is None:
            drifts.append(Drift(artifact, "missing", expected_digest, None, host))
        elif found_digest != expected_digest:
            drifts.append(Drift(artifact, "changed", expected_digest, found_digest, host))

    # 2. The dispatcher script: a separate deployment step from the package,
    # so it gets its own scope check and its own digest comparison.
    dispatcher_in_scope, _err, _checked = scope_of(DISPATCHER_UNIT_NAME)
    if dispatcher_in_scope is not False:
        expected_digest = _sha256_file(repo_root / DISPATCHER_RELATIVE_PATH)
        if expected_digest is not None:
            found_digest = _sha256_file(home / ".local" / "bin" / DISPATCHER_RELATIVE_PATH.name)
            artifact = f"dispatcher:{DISPATCHER_RELATIVE_PATH.name}"
            if found_digest is None:
                drifts.append(Drift(artifact, "missing", expected_digest, None, host))
            elif found_digest != expected_digest:
                drifts.append(Drift(artifact, "changed", expected_digest, found_digest, host))

    # 3. git_sha: the installed distribution's embedded commit hash.
    expected_sha = manifest.get("git_sha", "")
    found_sha = _installed_git_sha(home)
    if found_sha is None:
        drifts.append(Drift("git_sha", "missing", expected_sha, None, host))
    elif not _sha_matches(expected_sha, found_sha):
        drifts.append(Drift("git_sha", "changed", expected_sha, found_sha, host))

    # 4. Unit enablement versus activity: the timer case above is a latent
    # total outage no content digest would catch, because the unit file was
    # correct -- only its timers.target.wants symlink was missing. Checked
    # only for units in scope here (out-of-scope units are not supposed to
    # be enabled or active, so nothing to assert) that have a paired timer
    # at all.
    #
    # That "paired timer" test cannot be "does the repo ship one" alone:
    # the incident this whole check exists for (skfleet-rotate.timer active
    # but not enabled for at least 7 weeks on all three rotate hosts) is a
    # HAND-installed unit, in neither shipped tree, so it would never appear
    # in manifest["units"] and a shipped-only loop would silently never be
    # able to catch it again if it recurred. ALLOWED_UNSHIPPED_UNITS names
    # exactly the units that are real and live despite not being shipped
    # (reused from tests/fleet/test_no_dangling_systemd_unit_references.py,
    # the module docstring above explains why it is the one place that list
    # is allowed to live), so a service is also checked when both it and its
    # paired timer appear there.
    candidate_services = dict.fromkeys(manifest.get("units", []))
    candidate_services.update(
        dict.fromkeys(name for name in ALLOWED_UNSHIPPED_UNITS if name.endswith(".service"))
    )
    for unit_name in candidate_services:
        if not unit_name.endswith(".service"):
            continue
        timer_name = unit_name[: -len(".service")] + ".timer"
        shipped_timer = (repo_root / CANONICAL_SYSTEMD_RELATIVE_DIR / timer_name).exists()
        hand_installed_pair = (
            unit_name in ALLOWED_UNSHIPPED_UNITS and timer_name in ALLOWED_UNSHIPPED_UNITS
        )
        if not shipped_timer and not hand_installed_pair:
            continue
        in_scope, _scope_error, checked_unit = scope_of(unit_name)
        if in_scope is not True:
            continue  # out of scope, or undetermined: nothing to assert

        active_state, active_error = readiness._systemctl_show_value(checked_unit, "ActiveState")
        enabled_state, enabled_error = readiness._systemctl_show_value(
            checked_unit, "UnitFileState"
        )
        if active_error is not None or enabled_error is not None:
            continue

        active = active_state.strip() == "active"
        enabled = enabled_state.strip() in ("enabled", "enabled-runtime")
        artifact = f"unit_enablement:{checked_unit}"
        if active and not enabled:
            drifts.append(
                Drift(
                    artifact, "enablement_mismatch", "enabled", enabled_state.strip() or None, host
                )
            )
        elif enabled and not active:
            drifts.append(
                Drift(
                    artifact, "enablement_mismatch", "active", active_state.strip() or None, host
                )
            )

    return drifts
