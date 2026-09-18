"""Atlas brief publishing: render + write a static artifact per tick (R1.7)."""

from __future__ import annotations

from skcapstone.operator_seat import brief_publish

NOW = "2026-07-30T12:00:00Z"


def _firing_result():
    return {
        "frozen": False,
        "brief": {
            "firing": [
                {"app": "skchat", "type": "OutboxBounded", "status": "False", "object": "outbox"}
            ],
            "stale": [],
            "quiet": False,
            "counts": {"firing": 1, "stale": 0},
        },
        "route": "ornith",
        "outcomes": [{"action": "restart-daemon", "disposition": "auto", "outcome": "applied"}],
        "report": "operator: 1 firing on skchat",
    }


def _quiet_result():
    return {
        "frozen": False,
        "brief": {"firing": [], "stale": [], "quiet": True, "counts": {"firing": 0, "stale": 0}},
        "route": "quiet",
        "outcomes": [],
        "report": "all quiet",
    }


def test_html_renders_firing_state():
    out = brief_publish.render_html(_firing_result(), NOW)
    assert out.startswith("<!doctype html>")
    assert "Atlas operator brief" in out
    assert "skchat" in out and "OutboxBounded" in out
    assert "1 firing" in out
    assert "restart-daemon" in out


def test_html_renders_quiet_and_frozen():
    assert "ALL QUIET" in brief_publish.render_html(_quiet_result(), NOW)
    frozen = dict(_quiet_result(), frozen=True)
    assert "FROZEN" in brief_publish.render_html(frozen, NOW)


def test_html_escapes_untrusted_condition_text():
    evil = {
        "frozen": False,
        "brief": {
            "firing": [{"app": "<script>", "type": "x", "status": "False", "object": "y"}],
            "stale": [],
            "quiet": False,
            "counts": {"firing": 1, "stale": 0},
        },
        "route": "r",
        "outcomes": [],
        "report": "<b>not html</b>",
    }
    out = brief_publish.render_html(evil, NOW)
    assert "<script>" not in out  # escaped
    assert "&lt;script&gt;" in out
    assert "<b>not html</b>" not in out


def test_markdown_renders():
    md = brief_publish.render_markdown(_firing_result(), NOW)
    assert md.startswith("# Atlas operator brief")
    assert "`skchat` OutboxBounded (outbox) = False" in md
    assert "restart-daemon: auto -> applied" in md


def test_publish_writes_both_artifacts(tmp_path):
    written = brief_publish.publish_brief(_firing_result(), NOW, tmp_path / "brief")
    assert written["html"].read_text().startswith("<!doctype html>")
    assert written["markdown"].read_text().startswith("# Atlas operator brief")
    assert written["html"].name == "index.html"
    assert written["markdown"].name == "brief.md"


# ── tri-state freeze display (ACTUATION_READINESS_AND_FREEZE_STANDARD R4) ───


def _unprovisioned_result():
    return dict(_quiet_result(), freeze_state="unprovisioned")


def test_html_unprovisioned_never_reads_all_quiet():
    out = brief_publish.render_html(_unprovisioned_result(), NOW)
    assert "UNPROVISIONED" in out
    assert "ALL QUIET" not in out
    assert "not frozen" not in out.lower()


def test_markdown_unprovisioned_never_reads_all_quiet():
    out = brief_publish.render_markdown(_unprovisioned_result(), NOW)
    assert "UNPROVISIONED" in out
    assert "All quiet" not in out
    assert "not frozen" not in out.lower()


def test_three_freeze_states_render_distinctly():
    frozen = dict(_quiet_result(), frozen=True, freeze_state="frozen")
    active = dict(_quiet_result(), freeze_state="active")
    unprov = _unprovisioned_result()
    renders = {
        brief_publish.render_markdown(r, NOW).splitlines()[4] for r in (frozen, active, unprov)
    }
    assert len(renders) == 3
