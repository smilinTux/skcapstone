"""Static contract for governed seat runtime wiring in fleet rotation."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_link_prompt_requires_protected_merge_policy_and_exact_head() -> None:
    """Link receives the same fail-closed contract as the pure resolver."""
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()

    assert "LINK INTEGRATION PREFLIGHT:" in source
    assert "resolve_protected_merge_method" in source
    assert "squash, then rebase, then merge" in source
    assert "--match-head-commit" in source
    assert "If protected policy is unreadable" in source


def test_rotation_wires_link_reviewer_and_mero_in_order() -> None:
    """Review launches use all three governed runtime stages."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    link = source.index("recommend_reviewer(")
    reviewer = source.index("authorize_review_launch(", link)
    claim = source.index('claim=subprocess.run([SKC,"coord","claim"', reviewer)
    receipt = source.index("append_review_launch_receipt(", claim)
    mero = source.index("MeroObservation(", receipt)
    assert link < reviewer < claim < receipt < mero


def test_non_review_cards_bypass_assignment() -> None:
    """The integration returns unchanged ownership outside review cards."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    assert 'if "review" not in {str(label).strip().lower() for label in labels}:' in source
    assert "return reviewer, None, None" in source


def test_dry_run_exits_before_link_writes() -> None:
    """A selector dry run never appends a recommendation or observation."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    loop = source.index("for _pick_index,")
    dry = source.index("if DRY:", loop)
    assignment = source.index("_pool_v2_preclaim_handoff(", dry)
    assert dry < assignment


def test_link_and_jarvis_use_distinct_fresh_process_reads() -> None:
    """Assignment observes once, then authorization reads the process again."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    assignment = source[
        source.index("def _review_assignment(") : source.index("# Load this dependency-free")
    ]
    assert assignment.count("_card_process_snapshot(cid)") == 2
    assert "if any(observed_process.values()):" in assignment


def test_mero_tracks_review_lifecycle_without_mutation() -> None:
    """Oversight classifies active, complete, blocked, stale, and waiting."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    monitor = source[
        source.index("def _observe_assigned_reviews()") : source.index("if not picks:")
    ]
    for state in ("complete", "blocked", "active", "stale", "waiting"):
        assert f'state = "{state}"' in monitor
    assert "MeroObservation(" in monitor
    assert "coord claim" not in monitor
    assert "release-claim" not in monitor


def test_empty_pool_observes_terminal_reviews_before_exit() -> None:
    """Mero still records completion when there is no work to launch."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    empty_pool = source.index("if not picks:")
    observe = source.index("_observe_assigned_reviews()", empty_pool)
    exit_noop = source.index("sys.exit(0)", empty_pool)
    assert source.index("def _observe_assigned_reviews()") < empty_pool
    assert empty_pool < observe < exit_noop


def test_mero_blocked_and_stale_states_fail_closed() -> None:
    """Blocked evidence wins over liveness and a claimed dead worker is stale."""

    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    monitor = source[
        source.index("def _observe_assigned_reviews()") : source.index("if not picks:")
    ]
    complete = monitor.index('if lifecycle == "complete":')
    blocked = monitor.index('state = "blocked"')
    active = monitor.index('state = "active"')
    stale = monitor.index('state = "stale"')
    waiting = monitor.index('state = "waiting"')
    assert complete < blocked < active < stale < waiting


# ---------------------------------------------------------------------------
# WHERE THE ACTIVE DISPATCHER LIVES: this reverses PR #591 (2026-09-09).
#
# #591 moved every reference from ~/.local/bin to ~/.skenv/bin and locked it
# with the two tests below, on the rationale "a package upgrade cannot leave
# the active dispatcher outside the wheel". That goal is right. The premise
# turned out not to be: the migration never reached the hosts.
#
# Measured on chiap01/02/03/04/08 on 2026-09-19, ten days later, via
# `systemctl --user show skfleet-rotate.service -p ExecStart`: every one of
# them executes ~/.local/bin/skfleet-rotate.py. The deployed drop-in on every
# host had been hand-patched back, and the tracked template was the copy that
# had drifted. So for ten days the repo asserted one path while the fleet ran
# another, and two of the three consumers (the readiness gate and the
# niobe-live unit) were grading and launching a file that nothing runs.
#
# The operator's ruling (2026-09-19) is that ~/.local/bin is correct and the
# host hotfix stands. PR #808 brings the readiness gate and the drop-in
# template in line; this test pair is the last thing still asserting #591.
#
# #591's actual GOAL is not abandoned, it is met a different way. What #591
# wanted was that no upgrade can leave the active dispatcher stale. That is
# now enforced directly rather than by hoping pip's side effect covers it:
# deployment_manifest.PER_HOST_ARTIFACTS declares every per-host artifact
# once, staged_rollout generates a copy step per entry on BOTH the forward
# and the rollback path, and rollout_drift grades each deployed copy against
# the repo by content digest. A stale ~/.local/bin copy is now a reported
# finding; under #591 it was merely assumed not to exist.
#
# If the fleet is ever genuinely migrated to ~/.skenv/bin, the change is to
# PER_HOST_BIN_RELATIVE_DIR plus the hosts' units, and these assertions flip
# back with it.
# ---------------------------------------------------------------------------


def test_rotation_uses_the_deployed_runtime_interpreter() -> None:
    """The drop-in uses the venv interpreter and the DEPLOYED launcher.

    The interpreter stays wheel-owned (%h/.skenv/bin/python3); only the
    script path follows deployment. Those are separate questions and #591
    was right about the first one.
    """

    dropin = ROOT / "scripts/fleet/systemd/skfleet-rotate.service.d/seat-runtime-python.conf"
    assert dropin.read_bytes() == (
        b"[Service]\n"
        b"ExecStart=\n"
        b"ExecStart=%h/.skenv/bin/python3 %h/.local/bin/skfleet-rotate.py --go\n"
    )


def test_rotation_launcher_is_installed_by_the_wheel() -> None:
    """Every runtime file is still a script-files entry.

    Unchanged from #591 and still load-bearing: a file in no package is
    invisible to every version check, which is the skmail incident. What
    changed is only which COPY the units execute, asserted separately below.
    """

    pyproject = (ROOT / "pyproject.toml").read_text()
    for runtime_file in (
        "pi-cardstore-guard.mjs",
        "skfleet-pi-model-catalog.py",
        "skfleet-rotate.py",
        "skfleet-worker-wrapper.py",
        "skmail_work.py",
        "skmail_writer.py",
        "worktree-hygiene.py",
    ):
        assert f'"scripts/fleet/{runtime_file}"' in pyproject


def test_every_dispatcher_consumer_names_the_copy_the_fleet_runs() -> None:
    """One path, everywhere. Two dispatchers is the whole failure mode.

    The rotate service, the readiness gate and the niobe-live unit must all
    name the same file. When they disagree, the gate grades a copy nothing
    runs and niobe launches a second dispatcher that no rollout step keeps
    current -- which is how a fleet ends up running two different builds
    while every check reports green.
    """

    for unit in (
        ROOT / "systemd/skfleet-niobe-live.service",
        ROOT / "src/skcapstone/data/systemd/skfleet-niobe-live.service",
        ROOT / "systemd/skfleet-readiness.service",
        ROOT / "src/skcapstone/data/systemd/skfleet-readiness.service",
    ):
        text = unit.read_text()
        assert "%h/.local/bin/skfleet-rotate.py" in text, unit.name
        assert "%h/.skenv/bin/skfleet-rotate.py" not in text, unit.name


def test_tank_and_atlas_prompts_preserve_role_fences() -> None:
    """Seat prompts bind exact metadata and never grant ATLAS actuation."""
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    assert "approved artifact with sha256=%s" in source
    assert "Verify only target %s against evidence sha256=%s" in source
    assert "Do not deploy, dispatch, invoke an actuator" in source
    assert "Act only on the card-bound ActionIntent" not in source
