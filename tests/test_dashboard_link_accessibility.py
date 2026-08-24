from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "skdashboard" / "static"


def test_every_dashboard_surface_links_now_and_portfolio() -> None:
    for name in (
        "overview.html",
        "projects.html",
        "schedule.html",
        "board.html",
        "cockpit.html",
        "cmdb.html",
        "fleet.html",
        "economy.html",
        "models.html",
        "trust.html",
        "assistant.html",
    ):
        html = (STATIC / name).read_text()
        assert 'href="/control-plane/now"' in html, name
        assert 'href="/control-plane/portfolio?' in html, name
        assert 'href="/control-plane/schedule?' in html, name


def test_board_filters_have_explicit_accessible_labels() -> None:
    html = (STATIC / "board.html").read_text()
    for control in ("f-text", "f-owner", "f-kind", "f-priority"):
        assert f'for="{control}"' in html


def test_cockpit_and_cmdb_use_native_detail_buttons_and_managed_dialogs() -> None:
    cockpit = (STATIC / "js" / "cockpit.js").read_text()
    cmdb = (STATIC / "js" / "cmdb.js").read_text()
    helper = (STATIC / "js" / "detail_panel.js").read_text()
    for source in (cockpit, cmdb):
        assert 'type="button" data-' in source
        assert "createDetailPanel" in source
        assert "detailPanel.focusFirst()" in source
    assert 'event.key === "Escape"' in helper
    assert 'event.key !== "Tab"' in helper
    assert "trigger.focus()" in helper
    assert 'aria-label="Close details"' in cockpit
    assert 'aria-label="Close details"' in cmdb
    for source in (cockpit, cmdb):
        assert 'tabindex="-1" role="alert"' in source
        assert source.count("detailPanel.focusFirst()") >= 3


def test_mobile_styles_contain_wide_content() -> None:
    board = (STATIC / "css" / "board.css").read_text()
    cockpit = (STATIC / "css" / "cockpit.css").read_text()
    economy = (STATIC / "css" / "economy.css").read_text()
    assert "@media(max-width:560px)" in board
    assert "overflow-x:auto" in cockpit
    assert ".eco-table-wrap{max-width:100%}" in economy
    assert ".sevrow .fill{transition:none}" in cockpit
    assert ".ci{transition:none}" in (STATIC / "css" / "cmdb.css").read_text()
    assert ".eco-subtab{transition:none}" in economy
    assert "color:var(--ink2)" in cockpit
