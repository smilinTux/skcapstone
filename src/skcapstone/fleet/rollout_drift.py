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
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from .deployment_manifest import (
    CANONICAL_SYSTEMD_RELATIVE_DIR,
    PER_HOST_ARTIFACTS,
    PER_HOST_BIN_RELATIVE_DIR,
)
from .paths import paths_for_home, self_node_name

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
    wants-symlink state is wrong. ``failed`` covers a unit whose ActiveState
    is literally "failed": self-consistent with a "disabled" enablement
    state (so ``enablement_mismatch`` never fires for it), but unambiguous
    drift on its own terms -- no manifest or role knowledge is needed to
    know a crashed unit is wrong. This is the chiap08 incident: a seat unit
    sat FAILED for weeks and no existing check ever emitted a finding for
    it.
    """

    artifact: str
    kind: str  # "missing" | "changed" | "enablement_mismatch" | "failed"
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


def _sha256_script_body(path: Path) -> str | None:
    """The content digest of ``path``, ignoring a leading shebang line.

    Measured live on chiap01: pip rewrites a Python ``script-files`` entry's
    shebang (``#!/usr/bin/env python3`` becoming ``#!<venv>/bin/python``) on
    every install, and that install had otherwise-identical files that a
    byte-for-byte digest reported as "changed" on this one line alone. That
    rewrite is universal and harmless -- pip does it to every correctly
    installed host, so comparing raw bytes would make this check noisy on
    every host, not just a broken one, which is exactly the failure mode
    this whole module exists to avoid. A script with no shebang (the one
    ``.mjs`` entry) or one pip never rewrites (``skmail`` is bash; pip only
    rewrites a Python interpreter line) is unaffected: this strips at most
    one line from both sides of the comparison, so a genuine change deeper
    in the file is still caught exactly as before.
    """
    try:
        content = path.read_bytes()
    except OSError:
        return None
    if content.startswith(b"#!"):
        _, _, content = content.partition(b"\n")
    return hashlib.sha256(content).hexdigest()


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


def _checkout_git_sha(repo_root: Path) -> str | None:
    """The commit the node's own CHECKOUT is sitting on, or None.

    This is the THIRD version surface, and the one nothing was reading.
    ``_installed_git_sha`` above answers "what commit was the installed
    package built from"; the artifact digests answer "do the deployed
    copies match the checkout". Neither can answer "is the checkout itself
    the commit it is supposed to be" -- because every expected digest in
    this module is READ FROM that same checkout. A node whose checkout is
    stale therefore grades itself against its own stale content and reports
    a clean bill of health, on every surface, indefinitely.

    That is not hypothetical: on 2026-09-19 all five chi hosts reported a
    package version of ``dev222+gd448c2fa`` while their checkouts sat at a
    different commit, and no check in this module or the readiness gate
    emitted anything. It is also why ``detect_drift`` only reports this
    surface when the manifest was pinned by somebody else: see the
    ``checkout:git_sha`` block in ``detect_drift``.

    Returns None (never a fabricated sha) when git cannot answer, so a
    caller can tell "the checkout is at the wrong commit" from "there is no
    readable checkout here at all" -- two different problems with two
    different fixes, the same distinction ``_sha256_file`` draws for files.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short=8", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip()
    if result.returncode != 0 or not sha:
        return None
    return sha.lower()


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


def _script_files(repo_root: Path) -> list[str]:
    """Every entry under ``pyproject.toml``'s ``[tool.setuptools]
    script-files``.

    This is the exact list the skmail incident fell through: skmail was
    never in it, so pip never installed it as part of any package, and no
    version check could see a file that belonged to no package at all. That
    specific gap is closed (skmail is declared now), but the detector must
    not need a second, hand-typed copy of this list to stay honest about
    the NEXT script added the same way -- so it is read straight from
    pyproject.toml, the one place the list is declared, rather than
    duplicated here.

    Returns an empty list (never raises) when pyproject.toml is missing or
    malformed: a caller comparing an empty list finds nothing to check,
    which is a silent no-op, not a false "no drift" -- the git_sha and unit
    checks elsewhere in this module still run and still report a checkout
    that cannot be read as changed/missing on their own terms.
    """
    pyproject_path = repo_root / "pyproject.toml"
    try:
        with pyproject_path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    return list(data.get("tool", {}).get("setuptools", {}).get("script-files", []))


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


def detect_fleet_incoherence(home: Path | str) -> list[Drift]:
    """Do the estate's nodes agree with EACH OTHER about what they run?

    ``detect_drift`` answers "is THIS node internally consistent" and
    structurally cannot answer this one: every expected value it compares
    against is READ FROM the node's own checkout, so a host that never
    pulled agrees with itself perfectly, on every surface, forever. Five of
    them doing that at once is 2026-09-19, when all five chi hosts sat on a
    commit none of them had any way to notice was not the one being rolled
    out.

    Read from the readiness verdict each node already publishes every 15
    minutes to ``status/node-<host>/readiness/verdict.json`` under the one
    Syncthing folder the estate shares -- the same file
    ``staged_rollout._readiness_verdict`` already reads. So there is no ssh,
    no new publishing step and no second reporting channel; findings come
    back as ordinary ``Drift`` records that every existing reader handles
    unchanged.

    NOT read from ``rollout_history``, which is the obvious-looking
    substrate and the wrong one. That store is written only by
    ``staged_rollout.record_deployment``, and this fleet's deployments do
    not all go through it: measured on 2026-09-19, every node's recorded
    manifest said ``0c8dcd6b`` while every node's checkout was actually on
    ``112b2ef4``, about an hour stale. A check built on it would have
    answered "coherent" from records that agreed only because they were all
    equally out of date -- a confident wrong answer, which is worse than no
    answer. The readiness verdict is rewritten on a timer whether anything
    deployed or not, so it describes the node now.

    The rule is PLURALITY, not "compare everyone to me": the largest group
    agreeing on one sha is expected, and every node outside it is reported.
    A compare-to-me rule would report the three healthy hosts as drifted
    when run from the one stale host, so two operators reading the same
    shared tree would get two contradictory answers about the same estate.
    An exact tie is broken deterministically on the sha rather than on dict
    ordering.

    Fail-closed on a node that cannot say. A verdict that is absent,
    unreadable, or carries no ``installed_git_sha`` yields a ``missing``
    finding for that node, never silent agreement -- "unknown is never
    ready" is the rule the readiness gate itself is built on, and a
    coherence check that treats unknown as agreement is exactly the failure
    it exists to prevent. When NO node publishes the field, that is the one
    situation with a single cause (the gate publishing it is not deployed
    yet), so it is reported as one finding naming that cause rather than as
    a finding per node.

    Returns an empty list when fewer than two nodes publish a verdict at
    all: one node cannot disagree with itself.
    """
    home = Path(home)
    # Derived from the same paths_for_home the readiness gate's own writer
    # uses, via a throwaway probe path, so this can never disagree with it
    # about where the fleet status tree lives.
    status_root = paths_for_home(home).status_path("node", "readiness", "verdict").parents[2]
    if not status_root.is_dir():
        return []

    shas: dict[str, str] = {}
    unreadable: list[str] = []
    for node_dir in sorted(status_root.glob("node-*")):
        node = node_dir.name[len("node-") :]
        try:
            payload = json.loads(
                (node_dir / "readiness" / "verdict.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue  # no verdict at all: this node is not participating
        sha = payload.get("installed_git_sha") if isinstance(payload, dict) else None
        if isinstance(sha, str) and sha:
            shas[node] = sha.lower()
        else:
            unreadable.append(node)

    if not shas and not unreadable:
        return []
    if not shas:
        # Every participating node lacks the field: one cause, one finding.
        return [
            Drift(
                "fleet:installed_git_sha",
                "missing",
                "every node publishes installed_git_sha in its readiness verdict",
                f"no node does ({len(unreadable)} checked); the readiness gate that "
                "publishes it is not deployed yet",
                self_node_name(),
            )
        ]

    drifts = [
        Drift("fleet:installed_git_sha", "missing", "a published sha", None, node)
        for node in sorted(unreadable)
    ]
    if len(shas) < 2:
        return drifts

    groups: dict[str, list[str]] = {}
    for node, sha in shas.items():
        groups.setdefault(sha, []).append(node)
    if len(groups) == 1:
        return drifts

    # max() over (group size, reverse-ordered sha) keeps ties deterministic:
    # on an exact tie the lowest-sorted sha wins, so two hosts reading the
    # same tree report the same set.
    expected = max(groups, key=lambda sha: (len(groups[sha]), [-ord(c) for c in sha]))
    drifts += [
        Drift("fleet:installed_git_sha", "changed", expected, shas[node], node)
        for node in sorted(shas)
        if shas[node] != expected
    ]
    return drifts


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

    # 2. Every per-host artifact: each is a separate deployment step from
    # the package (an explicit ``cp`` into ~/.local/bin, see
    # staged_rollout._copy_steps), so the set gets its own scope check and
    # its own digest comparison.
    #
    # Driven by deployment_manifest.PER_HOST_ARTIFACTS, the same tuple the
    # rollout copies from, rather than naming the dispatcher alone. The
    # previous single-artifact version could not see a stale
    # ~/.local/bin/skfleet-worker-wrapper.py, and that file is what the
    # dispatcher actually execs: skfleet-rotate.py resolves it as
    # os.path.dirname(__file__)/skfleet-worker-wrapper.py, so the deployed
    # dispatcher always loads the wrapper sitting beside it, never the
    # package's copy in ~/.skenv/bin that section 2b below grades. A
    # rollout that shipped a new dispatcher calling an old wrapper would
    # have been invisible to every check in this module.
    #
    # Scoped by the DISPATCHER unit for the whole set on purpose: these
    # artifacts exist to serve skfleet-rotate.service, so a host that
    # genuinely does not carry that role carries none of them, and a
    # "missing" finding there would be a role difference, not drift --
    # exactly the distinction unit_in_scope exists to draw.
    #
    # Compared with _sha256_file, not _sha256_script_body: this copy is
    # made by ``cp``, which rewrites nothing, so the deployed bytes must
    # equal the repo's bytes exactly. (Section 2b's ~/.skenv/bin copies are
    # placed by pip, which DOES rewrite the shebang, hence the different
    # digest helper there.)
    dispatcher_in_scope, _err, _checked = scope_of(DISPATCHER_UNIT_NAME)
    if dispatcher_in_scope is not False:
        for relative_path in PER_HOST_ARTIFACTS:
            expected_digest = _sha256_file(repo_root / relative_path)
            if expected_digest is None:
                continue  # the repo no longer ships this artifact
            found_digest = _sha256_file(home / PER_HOST_BIN_RELATIVE_DIR / relative_path.name)
            artifact = f"dispatcher:{relative_path.name}"
            if found_digest is None:
                drifts.append(Drift(artifact, "missing", expected_digest, None, host))
            elif found_digest != expected_digest:
                drifts.append(Drift(artifact, "changed", expected_digest, found_digest, host))

    # 2b. Every other pyproject.toml script-files entry: pip installs each
    # one verbatim into ~/.skenv/bin by basename. This is the exact
    # mechanism section 2 above already trusts for the dispatcher script
    # (skfleet-rotate.py, one of these same entries, checked separately at
    # its OTHER install location, ~/.local/bin) -- an extension of that
    # existing content-digest pattern, not a new subsystem. This is the
    # incident that motivated this whole module: three different skmail
    # binaries across five hosts, none matching the repo, invisible to any
    # check because the file was never declared here and so belonged to no
    # package at all. skmail is declared now, but the NEXT script added the
    # same way must not need a second hand-typed list, so this reads
    # pyproject.toml directly (_script_files) rather than hand-enumerating.
    #
    # Compared with _sha256_script_body, not _sha256_file: pip rewrites a
    # Python script's shebang line to the venv's own interpreter on every
    # install (measured live on chiap01), which is universal and harmless,
    # never a fact about this host being wrong. See _sha256_script_body's
    # own docstring.
    for entry in _script_files(repo_root):
        expected_path = repo_root / entry
        expected_digest = _sha256_script_body(expected_path)
        if expected_digest is None:
            continue  # pyproject names a script the repo no longer ships
        basename = Path(entry).name
        found_digest = _sha256_script_body(home / ".skenv" / "bin" / basename)
        artifact = f"script:{basename}"
        if found_digest is None:
            drifts.append(Drift(artifact, "missing", expected_digest, None, host))
        elif found_digest != expected_digest:
            drifts.append(Drift(artifact, "changed", expected_digest, found_digest, host))

    # 3. package:git_sha -- the installed distribution's embedded commit
    # hash. This is the SECOND of the three version surfaces a chi host
    # carries, and the one the 2026-09-19 outage tripped over: an ad-hoc
    # deploy ran `git pull` and copied the dispatcher but skipped
    # `pip install -e .`, so the checkout moved, the script moved, and the
    # installed package did not. Named "package:" rather than the bare
    # "git_sha" it used to be, so a reader of a drift report can tell at a
    # glance WHICH surface is stale rather than having to know which of the
    # three a bare name refers to.
    expected_sha = manifest.get("git_sha", "")
    found_sha = _installed_git_sha(home)
    if found_sha is None:
        drifts.append(Drift("package:git_sha", "missing", expected_sha, None, host))
    elif not _sha_matches(expected_sha, found_sha):
        drifts.append(Drift("package:git_sha", "changed", expected_sha, found_sha, host))

    # 3b. checkout:git_sha -- the FIRST surface: the commit the node's own
    # checkout is on. Reported only when the caller pinned an expectation
    # from OUTSIDE this node (``manifest["checkout_git_sha_pinned"]``),
    # because when the manifest was built from this very checkout (the
    # plain ``fleet node drift`` path) the comparison is a tautology and
    # would be noise, not a signal.
    #
    # This is the only check here that can catch a UNIFORMLY stale node:
    # everything else in this module compares installed state against
    # repo_root, and repo_root IS the checkout, so a node that never pulled
    # agrees with itself perfectly on every other surface. It is also what
    # makes "do all five hosts agree with each other" answerable without a
    # second, fleet-wide reporting channel: gate every node against ONE
    # pinned manifest and cross-host agreement is a consequence, not a
    # separate report. ``staged_rollout._remote_drift`` does exactly that.
    if manifest.get("checkout_git_sha_pinned"):
        found_checkout = _checkout_git_sha(repo_root)
        if found_checkout is None:
            drifts.append(Drift("checkout:git_sha", "missing", expected_sha, None, host))
        elif not _sha_matches(expected_sha, found_checkout):
            drifts.append(Drift("checkout:git_sha", "changed", expected_sha, found_checkout, host))

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

    # 5. Unit health: a unit whose OWN ActiveState is "failed" is
    # unambiguous drift, full stop -- no manifest or role knowledge needed
    # to know a crashed unit is wrong. This is a genuinely different signal
    # from the enablement check above: that check reads the service's
    # PAIRED TIMER (unit_in_scope's own substitution for a ".service" name),
    # so a service that is FAILED but whose timer is healthy (active,
    # enabled) is invisible to it -- exactly the chiap08 shape
    # (skfleet-niobe-shadow.service FAILED for weeks, self-consistent with
    # a "disabled" UnitFileState, so the active-vs-enabled comparison above
    # never fires). No scope check is needed either: a unit whose role does
    # not apply to this host was never installed, so systemd reports it
    # inactive or unknown, never "failed" -- only a unit that genuinely ran
    # here and crashed reports "failed".
    all_service_names = dict.fromkeys(
        name for name in manifest.get("units", []) if name.endswith(".service")
    )
    all_service_names.update(
        dict.fromkeys(name for name in ALLOWED_UNSHIPPED_UNITS if name.endswith(".service"))
    )
    for unit_name in all_service_names:
        active_state, active_error = readiness._systemctl_show_value(unit_name, "ActiveState")
        if active_error is not None:
            continue
        if active_state.strip() == "failed":
            drifts.append(
                Drift(f"unit_failed:{unit_name}", "failed", "not failed", "failed", host)
            )

    return drifts
