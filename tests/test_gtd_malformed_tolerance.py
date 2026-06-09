"""GTD tools tolerate malformed (title/body, no 'text') items without crashing."""
import asyncio
import json
from pathlib import Path

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.mcp_tools.gtd_tools import _handle_gtd_done


def test_gtd_done_tolerates_item_without_text(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    gtd = tmp_path / "coordination" / "gtd"
    gtd.mkdir(parents=True)
    # A malformed legacy item: title/body schema, no 'text' (what the dead
    # reflection/improvement crons wrote and what used to KeyError gtd_done).
    (gtd / "inbox.json").write_text(
        json.dumps(
            [{"id": "m1", "title": "Daily Reflection", "body": "noise",
              "source": "daily-reflection-cron"}]
        ),
        encoding="utf-8",
    )

    # Must not raise (previously raised KeyError: 'text').
    result = asyncio.run(_handle_gtd_done({"item_id": "m1"}))
    assert result  # got a response

    inbox = json.loads((gtd / "inbox.json").read_text())
    assert not any(i.get("id") == "m1" for i in inbox)
    archive = json.loads((gtd / "archive.json").read_text())
    assert any(a.get("id") == "m1" for a in archive)
