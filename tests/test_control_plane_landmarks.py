from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src/skdashboard/static"
QUALIFIER = ROOT / "scripts/qualify_control_plane_accessibility_cdp.mjs"
PAGES = (
    "overview.html",
    "projects.html",
    "reliability.html",
    "architecture.html",
    "ai.html",
    "governance.html",
    "reports.html",
)


class _Landmarks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.main_depth = 0
        self.main_count = 0
        self.landmarks_overlap = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        is_main = tag == "main" or attributes.get("role") == "main"
        is_nav = tag == "nav" or attributes.get("role") == "navigation"
        if is_main:
            self.main_count += 1
            self.landmarks_overlap |= self.main_depth > 0
            self.main_depth += 1
        if is_nav:
            self.landmarks_overlap |= self.main_depth > 0

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self.main_depth -= 1


def test_each_qualified_page_has_one_non_nested_main_outside_navigation() -> None:
    for page in PAGES:
        parser = _Landmarks()
        source = (STATIC / page).read_text(encoding="utf-8")
        parser.feed(source)
        assert source.count("<main>") == source.count("</main>") == 1, page
        assert parser.main_count == 1, page
        assert parser.main_depth == 0, page
        assert not parser.landmarks_overlap, page
        nav = re.search(r'<(?P<tag>nav|div)[^>]+(?:role="navigation"|class="topbar)', source)
        assert nav, page
        assert (
            source[nav.end() : source.index("<main>")].rstrip().endswith(f"</{nav.group('tag')}>")
        ), page


def test_real_chrome_exposes_one_main_per_qualified_page(tmp_path: Path) -> None:
    node = shutil.which("node")
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not node or not chrome:
        pytest.skip("Node or Chrome is unavailable for the CDP landmark qualifier")
    subprocess.run(
        [node, str(QUALIFIER)],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "CHROME_PATH": chrome,
            "SKCP50_ARTIFACT_DIR": str(tmp_path),
        },
    )
    evidence = json.loads((tmp_path / "accessibility-landmark-matrix.json").read_text())
    assert evidence["result"] == "PASS"
    assert len(evidence["matrix"]) == 7
    assert all(row["mainLandmarks"] == row["axMain"] == 1 for row in evidence["matrix"])
