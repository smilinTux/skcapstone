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


def test_rotation_uses_the_deployed_runtime_interpreter() -> None:
    """The drop-in uses the venv interpreter and the DEPLOYED launcher.

    PR #591 (2026-09-09) set this to %h/.skenv/bin/skfleet-rotate.py, on the
    rationale that a package upgrade must not leave the active dispatcher
    outside the wheel. That migration never reached the hosts: measured on
    chiap01/02/03/04/08 on 2026-09-19 via `systemctl --user show
    skfleet-rotate.service -p ExecStart`, every host executes
    %h/.local/bin/skfleet-rotate.py. The deployed drop-in on each host had
    been hand-patched back and this tracked template was the copy that had
    drifted, so the repo asserted one path while the fleet ran another.

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

    Unchanged from PR #591 (2026-09-09) and still load-bearing: a file in no
    package is invisible to every version check (the incident that motivated
    #591 in the first place). What changed, below, is only which COPY the
    live units execute.
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


def test_niobe_live_dispatcher_names_the_deployed_copy() -> None:
    """The niobe-live unit must launch the copy the fleet actually runs.

    This reverses the second half of PR #591 (2026-09-09), which pointed
    `--dispatcher` at `%h/.skenv/bin/skfleet-rotate.py` on the rationale that
    a package upgrade must not leave the active dispatcher outside the
    wheel. That goal was right; the path was not. Measured on
    chiap01/02/03/04/08 on 2026-09-19 via `systemctl --user show
    skfleet-rotate.service -p ExecStart`, every host runs
    `%h/.local/bin/skfleet-rotate.py` - the drop-in template already agrees
    (see `test_rotation_uses_the_deployed_runtime_interpreter` above, fixed
    by #808), but this unit's `--dispatcher` flag still named the pip copy,
    which fails closed only because the wrong path also happens to exist
    (`niobe_live_entrypoint.run_live` raises "Niobe dispatcher is missing"
    only when the *named* path is absent, not when it is merely stale).

    The operator's 2026-09-19 ruling: `~/.local/bin` is canonical for every
    per-host fleet artifact. Do not "fix" this back to `.skenv/bin` - that
    would silently reintroduce the exact defect this test closes.

    This is a minimal, single-site fix. PR #811 (open at the time of this
    change, `feat/fleet-version-coherence`) declares
    `deployment_manifest.PER_HOST_ARTIFACTS` as the one place every per-host
    artifact's deployed path is derived from, and rewires this same unit
    plus `seat_cycle_entrypoint.py`'s two `sys.executable`-relative guesses
    through it; that is the intended long-term single source of truth and
    this test should be folded into its coverage once #811 merges, rather
    than kept as a second, hand-maintained assertion of the same fact.
    """

    for unit in (
        ROOT / "systemd/skfleet-niobe-live.service",
        ROOT / "src/skcapstone/data/systemd/skfleet-niobe-live.service",
    ):
        text = unit.read_text()
        assert "%h/.local/bin/skfleet-rotate.py" in text, unit
        assert "%h/.skenv/bin/skfleet-rotate.py" not in text, unit


def test_tank_and_atlas_prompts_preserve_role_fences() -> None:
    """Seat prompts bind exact metadata and never grant ATLAS actuation."""
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text()
    assert "approved artifact with sha256=%s" in source
    assert "Verify only target %s against evidence sha256=%s" in source
    assert "Do not deploy, dispatch, invoke an actuator" in source
    assert "Act only on the card-bound ActionIntent" not in source
