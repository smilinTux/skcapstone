"""Static contract for governed seat runtime wiring in fleet rotation."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


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
    loop = source.index("for _LANE,")
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
    assert 'if observed_process["sessions"]:' in assignment


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


def test_rotation_uses_the_packaged_runtime_interpreter() -> None:
    """The drop-in uses the wheel-owned interpreter and launcher."""

    dropin = ROOT / "scripts/fleet/systemd/skfleet-rotate.service.d/seat-runtime-python.conf"
    assert dropin.read_bytes() == (
        b"[Service]\n"
        b"ExecStart=\n"
        b"ExecStart=%h/.skenv/bin/python3 %h/.skenv/bin/skfleet-rotate.py --go\n"
    )


def test_rotation_launcher_is_installed_by_the_wheel() -> None:
    """A package upgrade cannot leave the active dispatcher outside the wheel."""

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

    for unit in (
        ROOT / "systemd/skfleet-niobe-live.service",
        ROOT / "src/skcapstone/data/systemd/skfleet-niobe-live.service",
    ):
        text = unit.read_text()
        assert "%h/.skenv/bin/skfleet-rotate.py" in text
        assert "%h/.local/bin/skfleet-rotate.py" not in text
